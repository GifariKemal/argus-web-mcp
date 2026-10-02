# Argus - self-contained image (server + Chromium). Used by docker-compose.yml so the
# whole MCP lives in Docker: container up = MCP up, container down = MCP down.
# Base pinned by digest: every push rebuilds this image, so a floating tag would ship
# whatever upstream published that hour. Bump the tag + digest deliberately.
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256

# Browsers and the embedding model live outside any home dir so the unprivileged runtime
# user can read them; root only installs.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright FASTEMBED_CACHE_PATH=/opt/fastembed
WORKDIR /app

# Dependencies first, from the hashed lockfile, so a code-only push reuses these layers
# instead of re-downloading ~1 GB. Regenerate requirements.lock with:
#   uv pip compile pyproject.toml --extra semantic --extra serve --universal \
#     --python-version 3.12 --generate-hashes -o requirements.lock
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Bake the rerank model (multilingual MiniLM, ~470MB) into the image. Downloading it on
# first use works, but makes startup depend on the HF Hub and logs an unauthenticated-
# request warning every time. Must match semantic.MODEL_NAME (test_semantic checks it).
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Run unprivileged. Chromium already runs with --no-sandbox (crawl4ai), so a renderer
# exploit would otherwise land as root. ~/.argus is created here so a fresh named
# volume mounted on it inherits this owner.
RUN useradd --create-home --uid 10001 argus \
    && mkdir -p /home/argus/.argus \
    && chown -R argus:argus /home/argus /opt/fastembed
USER argus

EXPOSE 8090
CMD ["uvicorn", "argus.server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
