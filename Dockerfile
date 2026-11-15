# Remote WhatsApp MCP: Go bridge + Python MCP server (OAuth over HTTPS) in one container.
# Mount a persistent volume at /app/whatsapp-bridge/store to keep the WhatsApp
# session, message history and OAuth tokens across deploys.
FROM golang:1.26-bookworm AS bridge
WORKDIR /src
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download
COPY whatsapp-bridge/*.go ./
RUN CGO_ENABLED=1 go build -o /out/whatsapp-bridge .

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY whatsapp-mcp-server/pyproject.toml whatsapp-mcp-server/uv.lock whatsapp-mcp-server/
RUN cd whatsapp-mcp-server && uv sync --frozen --no-dev
COPY whatsapp-mcp-server/*.py whatsapp-mcp-server/
COPY --from=bridge /out/whatsapp-bridge whatsapp-bridge/whatsapp-bridge
COPY scripts/start-remote.sh /app/start-remote.sh
ENV MCP_TRANSPORT=streamable-http \
    WHATSAPP_STORE_DIR=/app/whatsapp-bridge/store \
    PYTHONUNBUFFERED=1 \
    WHATSAPP_LOG_MESSAGES=0
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start-remote.sh"]
