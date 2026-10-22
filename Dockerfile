FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README_FA.md ./
COPY standardizer ./standardizer
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/standardized \
    && chown -R appuser:appuser /data
USER appuser

ENV STANDARDIZER_OUTPUT_DIR=/data/standardized \
    STANDARDIZER_DB_PATH=/data/standardizer.db \
    STANDARDIZER_OLLAMA_URL=http://ollama:11434

EXPOSE 8088
CMD ["uvicorn", "standardizer.api:app", "--host", "0.0.0.0", "--port", "8088"]
