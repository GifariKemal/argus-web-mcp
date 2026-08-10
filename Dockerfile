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

EXPOSE 8090
CMD ["uvicorn", "argus.server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
