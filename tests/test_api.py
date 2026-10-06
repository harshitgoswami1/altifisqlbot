import asyncio
import json

from fastapi.testclient import TestClient

from main import ChatService, Settings, create_app, encode_sse, stream_chat


class FakeService:
    def __init__(self, events=(), error=None):
        self.events = events
        self.error = error

    async def stream(self, question):
        assert question
        for event in self.events:
            await asyncio.sleep(0)
            yield event
        if self.error:
            raise self.error


def app_for(service, timeout=1, cors_origins=()):
    settings = Settings(
        database_url="postgresql://unused",
        ollama_base_url="http://unused",
        chat_timeout_seconds=timeout,
        cors_origins=cors_origins,
    )
    return create_app(service=service, settings=settings)


def parse_sse(body):
    parsed = []
    for frame in body.strip().split("\n\n"):
        lines = frame.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        parsed.append((lines[0][7:], json.loads(lines[1][6:])))
    return parsed


def test_request_validation():
    with TestClient(app_for(FakeService())) as client:
        assert client.post("/v1/chat/stream", json={}).status_code == 422
        assert client.post("/v1/chat/stream", json={"question": "   "}).status_code == 422
        assert (
            client.post("/v1/chat/stream", json={"question": "x" * 4_001}).status_code
            == 422
        )


def test_streams_valid_sse_in_order_and_finishes_with_done():
    service = FakeService(
        [
            ("citations", [{"chunk_id": "1", "title": "Bond", "url": "https://x.test"}]),
            ("token", {"text": "A fixed "}),
            ("token", {"text": "deposit."}),
        ]
    )
    with TestClient(app_for(service)) as client:
        with client.stream(
            "POST",
            "/v1/chat/stream",
            headers={"Accept": "text/event-stream"},
            json={"question": "What is a fixed deposit?"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream"
            assert response.headers["x-accel-buffering"] == "no"
            events = parse_sse("".join(response.iter_text()))

    assert events == [*service.events, ("done", {})]


def test_streaming_error_is_an_sse_event_without_done():
    with TestClient(app_for(FakeService(error=RuntimeError("secret")))) as client:
        response = client.post("/v1/chat/stream", json={"question": "hello"})
    assert response.status_code == 200
    assert parse_sse(response.text) == [
        ("error", {"detail": "Unable to generate an answer right now."})
    ]


def test_unavailable_backend_is_non_streaming_503():
    with TestClient(app_for(None)) as client:
        response = client.post("/v1/chat/stream", json={"question": "hello"})
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Backend is not ready."}


def test_configured_cors_origin_can_preflight_stream_endpoint():
    app = app_for(FakeService(), cors_origins=("http://127.0.0.1:5173",))
    with TestClient(app) as client:
        response = client.options(
            "/v1/chat/stream",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,accept",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_timeout_is_an_sse_error():
    class SlowService:
        async def stream(self, question):
            await asyncio.sleep(1)
            yield "token", {"text": "too late"}

    with TestClient(app_for(SlowService(), timeout=0.01)) as client:
        response = client.post("/v1/chat/stream", json={"question": "hello"})
    assert parse_sse(response.text) == [
        ("error", {"detail": "The request timed out after 0.01 seconds."})
    ]


def test_encode_sse_uses_json_and_blank_line_termination():
    frame = encode_sse("token", {"text": "hello\nworld"})
    assert frame.endswith(b"\n\n")
    assert parse_sse(frame.decode()) == [("token", {"text": "hello\nworld"})]


def test_service_turns_tool_urls_into_citations_and_model_chunks_into_tokens():
    class Chunk:
        def __init__(self, content):
            self.content = content

    class Agent:
        async def astream_events(self, payload, version):
            assert payload == {"messages": [{"role": "user", "content": "question"}]}
            assert version == "v2"
            yield {
                "event": "on_tool_end",
                "name": "query_database",
                "data": {"output": "source_url: https://example.com/bond"},
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": Chunk("answer")},
            }

    async def run():
        return [event async for event in ChatService(Agent()).stream("question")]

    assert asyncio.run(run()) == [
        ("citations", [{"url": "https://example.com/bond", "title": "https://example.com/bond"}]),
        ("token", {"text": "answer"}),
    ]


def test_first_chunk_is_available_before_answer_finishes():
    release = asyncio.Event()

    class StreamingService:
        async def stream(self, question):
            yield "token", {"text": "first"}
            await release.wait()
            yield "token", {"text": "second"}

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    async def run():
        stream = stream_chat(
            ConnectedRequest(), StreamingService(), "hello", 1, "test-request"
        )
        first = await anext(stream)
        release.set()
        rest = [chunk async for chunk in stream]
        return first, rest

    first, rest = asyncio.run(run())
    assert parse_sse(first.decode()) == [("token", {"text": "first"})]
    assert parse_sse(b"".join(rest).decode()) == [
        ("token", {"text": "second"}),
        ("done", {}),
    ]


def test_disconnect_closes_upstream_without_done_event():
    closed = asyncio.Event()

    class SlowService:
        async def stream(self, question):
            try:
                await asyncio.sleep(10)
                yield "token", {"text": "too late"}
            finally:
                closed.set()

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    async def run():
        return [
            chunk
            async for chunk in stream_chat(
                DisconnectedRequest(), SlowService(), "hello", 1, "test-request"
            )
        ]

    assert asyncio.run(run()) == []
    assert closed.is_set()
