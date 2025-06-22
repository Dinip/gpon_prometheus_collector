FROM python:3.13-alpine

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY prometheus_collector/ ./prometheus_collector/

RUN addgroup -g 1001 appgroup && \
    adduser -D -u 1001 -G appgroup appuser && \
    chown -R appuser:appgroup /app

USER appuser

ENTRYPOINT ["python3", "-m", "prometheus_collector.collector"]