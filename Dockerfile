FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/bot

COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[postgres]" || pip install --no-cache-dir httpx pydantic pydantic-settings sqlalchemy fastapi uvicorn "psycopg[binary]"

COPY app ./app
RUN useradd --create-home bot && mkdir -p /srv/bot/data && chown -R bot /srv/bot
USER bot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
