# Zeabur / Docker deploy: Playwright 官方 image 已內置 Chromium，唔使再裝 browser。
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py app.py ./

# Zeabur 會注入 $PORT；本地跑唔設就用 8077
ENV PORT=8077
EXPOSE 8077

CMD uvicorn app:app --host 0.0.0.0 --port $PORT
