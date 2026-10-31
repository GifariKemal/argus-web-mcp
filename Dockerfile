# Argus - self-contained image (server + Chromium). Used by docker-compose.yml so the
# whole MCP lives in Docker: container up = MCP up, container down = MCP down.
# Base pinned by digest: every push rebuilds this image, so a floating tag would ship
# whatever upstream published that hour. Bump the tag + digest deliberately.

# rs-trafilatura (Rust, MIT OR Apache-2.0): page-type routing and the extraction-quality
# score in extract/rs.py. Built from a pinned commit with our lockfile (upstream ships
# none, and its rust-toolchain.toml pins a Rust too old for the resolved crates); only the
# ~5 MB binary reaches the runtime stage. Debian trixie like the runtime, so glibc matches.
# Bumping: deploy/rs-trafilatura/README.md.
FROM rust:1.97.1-slim-trixie@sha256:8e8cf8f7fd54a2d23d5a743b3a03f56e26b6c774276c33fa0595111704ebb15c AS rs-trafilatura
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q . \
    && git fetch -q --depth 1 https://github.com/Murrough-Foley/rs-trafilatura.git \
       2a4f56eef84cca2d319d27c9108373d593110355 \
    && git checkout -q FETCH_HEAD && rm rust-toolchain.toml
COPY deploy/rs-trafilatura/Cargo.lock ./
RUN cargo build --release --locked --bin extract_stdin

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
# apt upgrade: Debian security fixes land before the next base-digest bump (trivy gate).
# tesseract (+ English and Indonesian data, ~20 MB): OCR for image URLs and scanned PDF
# pages (extract/ocr.py). Without it those reads report "ocr unavailable".
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       tesseract-ocr tesseract-ocr-eng tesseract-ocr-ind \
    && pip install --no-cache-dir --require-hashes -r requirements.lock \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Bake the rerank model (multilingual MiniLM, ~470MB) into the image. Downloading it on
# first use works, but makes startup depend on the HF Hub and logs an unauthenticated-
# request warning every time. Must match semantic.MODEL_NAME (test_semantic checks it).
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY --from=rs-trafilatura /src/target/release/extract_stdin /usr/local/bin/rs-trafilatura

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
