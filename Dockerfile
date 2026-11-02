FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app

COPY Navidrome/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY Navidrome /app/Navidrome
RUN rm -f /app/Navidrome/config.json && chmod +x /app/Navidrome/entrypoint.sh

WORKDIR /app/Navidrome
ENTRYPOINT ["/app/Navidrome/entrypoint.sh"]
