FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends supervisor curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY supervisord.conf ./

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

RUN useradd -m appuser \
 && mkdir -p /data \
 && chown appuser:appuser /data
USER appuser

EXPOSE 8000
EXPOSE 53/udp
EXPOSE 53/tcp

CMD ["supervisord", "-c", "/app/supervisord.conf", "-n"]
