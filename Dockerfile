FROM python:3.14.3-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && groupadd --system autocut \
    && useradd --system --gid autocut --home-dir /app autocut

COPY --chown=autocut:autocut src/ ./src/

USER autocut
EXPOSE 8000
CMD ["uvicorn", "autocut_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
