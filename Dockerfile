# Argus - self-contained image (server + Chromium). Used by docker-compose.yml so the
# whole MCP lives in Docker: container up = MCP up, container down = MCP down.
# Base pinned by digest: every push rebuilds this image, so a floating tag would ship
# whatever upstream published that hour. Bump the tag + digest deliberately.
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Dependencies first, from the hashed lockfile, so a code-only push reuses these layers
# instead of re-downloading ~1 GB. Regenerate requirements.lock with:
#   uv pip compile pyproject.toml --extra semantic --extra serve --universal \
#     --python-version 3.12 --generate-hashes -o requirements.lock
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Bake the rerank model (~130MB) into the image. Downloading it on first use works,
# but that makes startup depend on the HF Hub being reachable and logs an
# unauthenticated-request warning every time. Baked = offline start, quiet logs.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8090
CMD ["uvicorn", "argus.server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
