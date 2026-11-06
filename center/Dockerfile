FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app/center
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY . .
RUN uv sync --locked --no-dev --no-cache \
    && useradd --create-home --uid 10001 driver
ENV PATH="/app/center/.venv/bin:$PATH"
USER driver
EXPOSE 8000/tcp 9999/udp
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["auto-driver-center"]
