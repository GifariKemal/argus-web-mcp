# Argus - self-contained image (server + Chromium). Used by docker-compose.yml so the
# whole MCP lives in Docker: container up = MCP up, container down = MCP down.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

# `semantic` extra = local rerank/find_similar (ONNX, no torch).
RUN pip install --no-cache-dir ".[semantic]" uvicorn \
    && playwright install --with-deps chromium

# Bake the rerank model (~130MB) into the image. Downloading it on first use works,
# but that makes startup depend on the HF Hub being reachable and logs an
# unauthenticated-request warning every time. Baked = offline start, quiet logs.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

EXPOSE 8090
CMD ["uvicorn", "argus.server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
