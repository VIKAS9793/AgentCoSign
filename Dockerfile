# Hermetic by default: runs with no credentials (recorded LLM + recorded sandbox).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENTCOSIGN_HOST=0.0.0.0
WORKDIR /app
COPY pyproject.toml README.md LICENSE config.yaml ./
COPY agentcosign ./agentcosign
RUN pip install --no-cache-dir . && useradd -r -u 10001 app && mkdir -p /app/data && chown app /app/data
USER app
# Render injects PORT (default 10000); locally and in plain Docker it falls back to 8000.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=2)"
CMD ["sh", "-c", "exec uvicorn agentcosign.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
