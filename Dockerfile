FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
ENV KSA_CONFIG=config/settings.yaml KSA_DB_PATH=/app/data/assistant.db KSA_LOG_LEVEL=WARNING
VOLUME /app/data
# Secrets are passed at runtime: docker run --env-file .env ...
CMD ["kalshi-assistant", "monitor", "--loop", "--interval", "60"]
