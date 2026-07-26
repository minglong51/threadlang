FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system threadlang \
    && useradd --system --gid threadlang --home-dir /app threadlang

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN mkdir -p /data && chown threadlang:threadlang /data
USER threadlang
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2).read()"

ENTRYPOINT ["threadlang-serve"]
CMD ["--store", "/data/threadlang.db", "--host", "0.0.0.0", "--port", "8765"]
