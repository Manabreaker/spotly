FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY spotly ./spotly
COPY migrations ./migrations
COPY alembic.ini ./
RUN useradd --uid 10001 --create-home app && mkdir -p /app/data/photos && chown -R app:app /app/data
USER app
EXPOSE 8000
CMD ["sh","-c","alembic upgrade head && exec uvicorn spotly.main:build_app --factory --host 0.0.0.0 --port 8000 --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
