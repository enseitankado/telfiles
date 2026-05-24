# Slim base: just Python + glibc + apt. We add only the Chromium runtime
# libraries we actually need, instead of inheriting Microsoft's playwright
# image which also bundles Firefox, WebKit, a stock Chromium binary, every
# locale and a fat font set — none of which Stage 2 uses. End result is
# ~half the on-disk size with identical behaviour.
FROM python:3.10-slim

WORKDIR /app

# HF_HOME persists the sentence-transformers model cache to a known path
# inside the image (and any volume mount overriding it).
ENV CLOAKBROWSER_AUTO_UPDATE=false \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/models \
    EMBED_MODEL=intfloat/multilingual-e5-small

# Install torch CPU-only first (CUDA build is ~2 GB and unnecessary in a
# headless container). sentence-transformers picks up the already-installed
# torch from this step instead of pulling the default GPU build.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium's shared-library dependencies. We delegate the list to
# Playwright's own `install-deps chromium` resolver — it carries the
# canonical, version-tracked apt-package list for the Chromium build
# CloakBrowser ships. Then immediately drop the apt list cache to keep
# the layer small.
#
# aria2 ships alongside it: used by magnet_metadata.py to fetch BitTorrent
# metadata via DHT/peers ("--bt-metadata-only") so we can list files inside
# a magnet without fully downloading anything.
RUN apt-get update \
 && playwright install-deps chromium \
 && apt-get install -y --no-install-recommends aria2 \
 && rm -rf /var/lib/apt/lists/*

# Pre-download the patched Chromium binary so the first Stage 2 run after
# container startup doesn't have to fetch ~200 MB before any scraping
# happens. The binary lands under /root/.cache/cloakbrowser and stays
# inside the image layer.
RUN python -c "from cloakbrowser import ensure_binary; ensure_binary()"

# Pre-cache the multilingual-e5-small embedding model (~120 MB) so the
# first semantic search after container start is instant and works
# offline. The model lands under /app/models (HF_HOME).
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('intfloat/multilingual-e5-small')"

COPY . .

RUN mkdir -p /app/data /app/downloads /app/models

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
