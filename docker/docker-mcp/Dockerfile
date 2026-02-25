FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    rustc \
    cargo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./

RUN python -m venv /build/venv
ENV PATH="/build/venv/bin:$PATH"
ENV PIP_PREFER_BINARY=1
ENV PIP_INDEX_URL=https://pypi.org/simple

RUN pip install --no-cache-dir maturin typing-extensions && \
    printf '%s\n' '#!/bin/sh' 'exec /build/venv/bin/python -m maturin "$@"' > /usr/local/bin/maturin && \
    chmod +x /usr/local/bin/maturin && \
    /usr/local/bin/maturin --version && \
    pip install --no-cache-dir --no-build-isolation --no-binary pydantic-core,rpds-py -r requirements.txt

RUN python -c "import pydantic, pydantic_core, mcp, httpx; print('mcp deps ok', pydantic.__version__)"

FROM python:3.11-slim

COPY --from=builder /build/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY app/ ./app/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8081
ENV FLASK_API_BASE=http://wainlux-k6:8080

EXPOSE 8081

CMD ["python3", "app/mcp_server.py"]
