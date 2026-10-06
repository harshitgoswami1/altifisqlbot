import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlparse

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_ollama import ChatOllama
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from prompts import SYSTEM_PROMPT

load_dotenv()

TABLE_NAME = "scraped_bonds"
URL_RE = re.compile(r"https?://[^\s'\",)}\]]+")
logger = logging.getLogger("sqlagentbackend")


def log_event(level: int, event: str, **fields: Any) -> None:
    logger.log(
        level,
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                **fields,
            },
            default=str,
            separators=(",", ":"),
        ),
    )


@dataclass(frozen=True)
class Settings:
    database_url: str
    ollama_base_url: str
    model: str = "qwen2.5:7b"
    db_connect_timeout_seconds: int = 10
    db_statement_timeout_seconds: int = 30
    chat_timeout_seconds: float = 120.0
    startup_timeout_seconds: float = 20.0
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL", "").strip()
        # Kept as a fallback because brute.ipynb uses OLLAMA_API_KEY as base_url.
        ollama_base_url = (
            os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_API_KEY") or ""
        ).strip()
        missing = [
            name
            for name, value in (
                ("DATABASE_URL", database_url),
                ("OLLAMA_BASE_URL (or OLLAMA_API_KEY)", ollama_base_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required environment variable(s): {', '.join(missing)}")

        return cls(
            database_url=database_url,
            ollama_base_url=ollama_base_url,
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b").strip() or "qwen2.5:7b",
            db_connect_timeout_seconds=int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "10")),
            db_statement_timeout_seconds=int(
                os.getenv("DB_STATEMENT_TIMEOUT_SECONDS", "30")
            ),
            chat_timeout_seconds=float(os.getenv("CHAT_TIMEOUT_SECONDS", "120")),
            startup_timeout_seconds=float(os.getenv("STARTUP_TIMEOUT_SECONDS", "20")),
            cors_origins=tuple(
                origin.strip()
                for origin in os.getenv("CORS_ORIGINS", "").split(",")
                if origin.strip()
            ),
        )


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=4_000)


class ChatService:
    def __init__(self, agent: Any):
        self.agent = agent

    @classmethod
    def build(cls, settings: Settings) -> "ChatService":
        schema = get_table_schema(settings)
        query_database = make_query_tool(settings)
        columns_for_prompt = "\n".join(
            f"- {column['column_name']} ({column['data_type']})" for column in schema
        )
        model = ChatOllama(model=settings.model, base_url=settings.ollama_base_url)
        agent = create_agent(
            model=model,
            tools=[query_database],
            system_prompt=SYSTEM_PROMPT.format(
                table_name=TABLE_NAME,
                columns=columns_for_prompt,
            ),
        )
        return cls(agent)

    async def stream(self, question: str) -> AsyncIterator[tuple[str, Any]]:
        seen_urls: set[str] = set()
        async for event in self.agent.astream_events(
            {"messages": [{"role": "user", "content": question}]}, version="v2"
        ):
            if event["event"] == "on_tool_end" and event.get("name") == "query_database":
                output = event.get("data", {}).get("output", "")
                content = getattr(output, "content", output)
                urls = [url.rstrip(".") for url in URL_RE.findall(str(content))]
                citations = []
                for url in urls:
                    if url not in seen_urls:
                        seen_urls.add(url)
                        citations.append({"url": url, "title": url})
                if citations:
                    yield "citations", citations
            elif event["event"] == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                text = message_text(chunk)
                if text:
                    yield "token", {"text": text}


def message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def get_table_schema(settings: Settings) -> list[dict[str, Any]]:
    parsed_url = urlparse(settings.database_url)
    schema_name = parse_qs(parsed_url.query).get("schema", ["public"])[0]
    query = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
    """
    with psycopg.connect(
        settings.database_url,
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout_seconds,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (schema_name, TABLE_NAME))
            columns = cursor.fetchall()
    if not columns:
        raise ValueError(f"Could not find table {schema_name}.{TABLE_NAME}.")
    return columns


def make_query_tool(settings: Settings):
    @tool
    async def query_database(sql: str) -> str:
        """Run one read-only SELECT query against the configured PostgreSQL table.

        Use this tool to answer questions about records in the database.
        Only query the configured table and its columns. Do not modify data.
        """

        sql = sql.strip().rstrip(";")
        if not re.match(r"^(SELECT|WITH)\b", sql, flags=re.IGNORECASE):
            return "Rejected: only SELECT queries are allowed."
        if ";" in sql or "--" in sql or "/*" in sql:
            return "Rejected: multiple statements and SQL comments are not allowed."
        if not re.search(r"\bLIMIT\s+\d+\b", sql, flags=re.IGNORECASE):
            sql += " LIMIT 100"

        try:
            connection = await psycopg.AsyncConnection.connect(
                settings.database_url,
                row_factory=dict_row,
                connect_timeout=settings.db_connect_timeout_seconds,
            )
            async with connection:
                await connection.execute("SET TRANSACTION READ ONLY")
                await connection.execute(
                    f"SET LOCAL statement_timeout = '{settings.db_statement_timeout_seconds}s'"
                )
                async with connection.cursor() as cursor:
                    await cursor.execute(sql)
                    return str(await cursor.fetchall())
        except Exception as exc:
            return f"Query failed: {exc}"

    return query_database


def encode_sse(event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


async def wait_for_disconnect(request: Request) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(0.1)


async def stream_chat(
    request: Request,
    service: Any,
    question: str,
    timeout_seconds: float,
    request_id: str,
) -> AsyncIterator[bytes]:
    iterator = service.stream(question).__aiter__()
    disconnect_task = asyncio.create_task(wait_for_disconnect(request))
    next_task: asyncio.Task | None = None
    log_event(logging.INFO, "chat_started", request_id=request_id)
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                next_task = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait(
                    {next_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    next_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await next_task
                    log_event(logging.INFO, "chat_cancelled", request_id=request_id)
                    return
                try:
                    event, data = next_task.result()
                except StopAsyncIteration:
                    break
                yield encode_sse(event, data)

        yield encode_sse("done", {})
        log_event(logging.INFO, "chat_completed", request_id=request_id)
    except TimeoutError:
        log_event(logging.WARNING, "chat_timed_out", request_id=request_id)
        yield encode_sse(
            "error", {"detail": f"The request timed out after {timeout_seconds:g} seconds."}
        )
    except asyncio.CancelledError:
        log_event(logging.INFO, "chat_cancelled", request_id=request_id)
        raise
    except Exception as exc:
        log_event(
            logging.ERROR,
            "chat_failed",
            request_id=request_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        yield encode_sse(
            "error", {"detail": "Unable to generate an answer right now."}
        )
    finally:
        if next_task and not next_task.done():
            next_task.cancel()
            with suppress(asyncio.CancelledError):
                await next_task
        disconnect_task.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect_task
        aclose = getattr(iterator, "aclose", None)
        if aclose:
            with suppress(Exception):
                await aclose()


_DEFAULT_SERVICE = object()


def create_app(
    service: Any = _DEFAULT_SERVICE,
    settings: Settings | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not _DEFAULT_SERVICE:
            app.state.chat_service = service
            app.state.startup_error = None
            if settings:
                app.state.settings = settings
        else:
            try:
                current_settings = settings or Settings.from_env()
                async with asyncio.timeout(current_settings.startup_timeout_seconds):
                    app.state.chat_service = await asyncio.to_thread(
                        ChatService.build, current_settings
                    )
                app.state.startup_error = None
                app.state.settings = current_settings
                log_event(logging.INFO, "backend_ready", model=current_settings.model)
            except Exception as exc:
                app.state.chat_service = None
                app.state.startup_error = "Backend initialization failed. Check server logs."
                log_event(
                    logging.ERROR,
                    "backend_startup_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        yield

    app = FastAPI(title="SQL Agent Backend", lifespan=lifespan)
    configured_origins = settings.cors_origins if settings else tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "").split(",")
        if origin.strip()
    )
    if configured_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(configured_origins),
            allow_methods=["POST"],
            allow_headers=["Content-Type", "Accept"],
        )

    @app.post("/v1/chat/stream")
    async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
        chat_service = request.app.state.chat_service
        if chat_service is None:
            raise HTTPException(
                status_code=503,
                detail=request.app.state.startup_error or "Backend is not ready.",
            )

        active_settings = getattr(request.app.state, "settings", settings)
        timeout = active_settings.chat_timeout_seconds if active_settings else 120.0
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        return StreamingResponse(
            stream_chat(request, chat_service, payload.question, timeout, request_id),
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
            },
        )

    return app


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
app = create_app()
