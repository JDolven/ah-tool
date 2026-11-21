# AH Tool web version: the shared, read-mostly site the local app pushes market data to (see README, "Web version").
# Standard library only, so the image is just Python + the source. The tests run during the build: an image that
# would leak your own trades or mix up visitors' state is never built.
FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY ahtool ahtool
COPY web web
COPY tests tests
COPY run_web.py ./

RUN mkdir -p data && for t in tests/test_*.py; do python "$t" >/tmp/test.log 2>&1 || { tail -40 /tmp/test.log; exit 1; }; done \
    && rm -rf data tests /tmp/test.log && find / -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null; true

EXPOSE 8642
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD wget -qO- http://127.0.0.1:8642/healthz >/dev/null || exit 1
CMD ["python", "run_web.py"]
