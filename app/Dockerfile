# Slim base: just Python + glibc + apt. We add only the Chromium runtime
# libraries we actually need, instead of inheriting Microsoft's playwright
# image which also bundles Firefox, WebKit, a stock Chromium binary, every
# locale and a fat font set — none of which Stage 2 uses. End result is
# ~half the on-disk size with identical behaviour.
FROM python:3.10-slim

WORKDIR /app

ENV CLOAKBROWSER_AUTO_UPDATE=false \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium's shared-library dependencies. We delegate the list to
# Playwright's own `install-deps chromium` resolver — it carries the
# canonical, version-tracked apt-package list for the Chromium build
# CloakBrowser ships. Then immediately drop the apt list cache to keep
# the layer small.
RUN apt-get update \
 && playwright install-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# Pre-download the patched Chromium binary so the first Stage 2 run after
# container startup doesn't have to fetch ~200 MB before any scraping
# happens. The binary lands under /root/.cache/cloakbrowser and stays
# inside the image layer.
RUN python -c "from cloakbrowser import ensure_binary; ensure_binary()"

COPY . .

RUN mkdir -p /app/data /app/downloads

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
