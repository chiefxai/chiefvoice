FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home chiefvoice

USER 10001:10001
EXPOSE 8080

CMD ["sh", "-c", "uvicorn chiefvoice.app:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
