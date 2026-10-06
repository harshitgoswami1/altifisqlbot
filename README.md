# SQL agent backend

FastAPI wrapper for the PostgreSQL + Ollama agent in `notebooks/brute.ipynb`.
It exposes the frontend contract at `POST /v1/chat/stream` and streams JSON SSE
events (`citations`, `token`, then `done`; or `error` after streaming begins).

## Local startup

Python 3.12+ and PostgreSQL are required. The configured database must contain
the `scraped_bonds` table (in the URL's configured schema, or `public`), and the
Ollama-compatible endpoint must have the configured model available.

```sh
cp .env.example .env
# Edit DATABASE_URL and OLLAMA_BASE_URL.
uv sync --dev
uv run uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

The notebook used `OLLAMA_API_KEY` as the `ChatOllama.base_url`. The backend
continues to accept that name for compatibility, but `OLLAMA_BASE_URL` is the
clearer preferred name.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | required | PostgreSQL connection URL |
| `OLLAMA_BASE_URL` | `OLLAMA_API_KEY` fallback | Ollama-compatible base URL |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Chat model |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | Database connection deadline |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `30` | PostgreSQL query deadline |
| `CHAT_TIMEOUT_SECONDS` | `120` | Whole streamed-answer deadline |
| `STARTUP_TIMEOUT_SECONDS` | `20` | Schema inspection/agent startup deadline |
| `CORS_ORIGINS` | empty | Comma-separated allowed frontend origins |
| `LOG_LEVEL` | `INFO` | Application log level |

Run the tests with:

```sh
uv run python -m pytest
```

## Test the stream

`curl -N` disables client-side response buffering:

```sh
curl -N http://127.0.0.1:8000/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  --data '{"question":"What is a fixed deposit?"}'
```

Cancel the request with Ctrl-C. The server detects the disconnect and closes the
upstream agent stream.

## Frontend and production proxy

For the existing Vite development proxy, leave `VITE_API_BASE_URL` empty and set:

```sh
VITE_BACKEND_TARGET=http://127.0.0.1:8000
```

In production, use a same-origin reverse proxy for `/v1/`. SSE buffering must be
disabled and proxy read timeouts must exceed `CHAT_TIMEOUT_SECONDS`. An nginx
location can be configured as:

```nginx
location /v1/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 130s;
}
```

If the frontend is served from another origin, set its absolute
`VITE_API_BASE_URL` and list the exact origin in `CORS_ORIGINS`; do not use `*`
for production.

## Scope assumptions

This preserves the notebook's single-question, stateless behavior and its
`scraped_bonds` table. It does not add authentication, accounts, conversation
history, persistence, uploads, or WebSockets. Add those only after defining their
authorization, retention, and API requirements; none are needed by the current
frontend contract.
