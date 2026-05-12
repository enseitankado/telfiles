# Microsoft's official Playwright Python image: includes Chromium + all of
# Chromium's runtime libs (libnss, libatk, fonts, …) pre-installed plus a
# matching Python interpreter. We use Chromium in Stage 2 of the Channel
# Hunter to bypass anti-bot pages (Cloudflare/CAPTCHA) that plain aiohttp
# can't get through. Image size is ~2.3 GB which is a 7× jump over slim,
# but the alternative is "stage 2 doesn't work" for most search engines.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/downloads

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
