# Microsoft's official Playwright Python image: includes a base Chromium
# plus every shared lib (libnss, libatk, fonts, …) we need at runtime. We
# don't actually use the stock Chromium for scraping — CloakBrowser ships
# its own patched build — but the system libraries that come with this
# image are exactly what that patched binary needs to run, so it's still
# the cleanest base.
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
