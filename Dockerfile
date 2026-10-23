FROM python:3.14.3-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && groupadd --system autocut \
    && useradd --system --gid autocut --home-dir /app autocut \
    && mkdir -p /data \
    && chown autocut:autocut /data

COPY --chown=autocut:autocut src/ ./src/

USER autocut
EXPOSE 8000

CMD ["sh", "-c", "python -c 'import os,sys; sys.exit(0 if os.getenv(\"API_TOKEN\", \"\").strip() else 1)' || { echo 'API_TOKEN must be set to a non-whitespace value.' >&2; exit 1; }; exec uvicorn autocut_api.main:app --host 0.0.0.0 --port 8000"]
