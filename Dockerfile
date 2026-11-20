# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SDG_CACHE_PATH=/data/cache.sqlite3 \
    HF_HOME=/models \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Keep dependency layers cached when only application code changes.
COPY requirements.txt requirements-openalex-local.txt ./
# CPU wheels avoid downloading the CUDA runtime on Linux VPS hosts.
RUN python -m pip install --no-cache-dir 'torch==2.14.1' --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install -r requirements.txt -r requirements-openalex-local.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data /models /app/.streamlit \
    && chown -R app:app /data /models /app

COPY --chown=app:app app.py cache_db.py fetch_jobs.py llm_sdg.py openalex_local_sdg.py openalex_sdg.py publication_sources.py request_utils.py ./
COPY --chown=app:app dspace_sources.toml oai_sources.toml ./
COPY --chown=app:app models/openalex-sdgs/ ./models/openalex-sdgs/
COPY --chown=app:app .streamlit/config.toml ./.streamlit/config.toml

USER app
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"

CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
