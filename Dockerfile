# Hermetic by default: runs with no credentials (recorded LLM + recorded sandbox).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENTCOSIGN_HOST=0.0.0.0 AGENTCOSIGN_PORT=8000
WORKDIR /app
COPY pyproject.toml README.md LICENSE config.yaml ./
COPY agentcosign ./agentcosign
RUN pip install --no-cache-dir . && useradd -r -u 10001 app && mkdir -p /app/data && chown app /app/data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "agentcosign.main:app", "--host", "0.0.0.0", "--port", "8000"]
