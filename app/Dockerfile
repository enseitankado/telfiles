# Base image kept as Microsoft's mcr.microsoft.com/playwright/python only
# for the Chromium runtime libraries it ships (libnss, libatk, libgbm,
# fonts, X libs …). We do NOT use the bundled stock Chromium — Stage 2
# scraping runs on CloakBrowser's patched Chromium build, downloaded into
# this image during the next RUN. Using this base just avoids a 200-line
# apt-get install for Chromium's runtime dependencies.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# Disable CloakBrowser's auto-update check at runtime — we pin a known
# binary at build time and don't want background HTTP fetches in prod.
ENV CLOAKBROWSER_AUTO_UPDATE=false

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the patched Chromium binary so the first Stage 2 run after
# container startup doesn't have to fetch ~200 MB before any scraping
# happens. The binary lands under /root/.cache/cloakbrowser (or wherever
# CLOAKBROWSER_CACHE_DIR points) and stays inside the image layer.
RUN python -c "from cloakbrowser import ensure_binary; ensure_binary()"

COPY . .

RUN mkdir -p /app/data /app/downloads

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
