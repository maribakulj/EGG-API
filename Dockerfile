FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m pip install --upgrade pip build \
 && python -m build --wheel --outdir /wheels


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EGG_HOME=/var/lib/egg \
    EGG_ENV=production

RUN groupadd --system --gid 1000 egg \
 && useradd --system --uid 1000 --gid 1000 --home-dir /var/lib/egg --shell /usr/sbin/nologin egg \
 && mkdir -p /var/lib/egg/data /var/lib/egg/config \
 && chown -R egg:egg /var/lib/egg

COPY --from=builder /wheels /tmp/wheels
RUN python -m pip install /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels

USER egg
WORKDIR /var/lib/egg

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
