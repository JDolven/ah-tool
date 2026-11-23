# AH Tool web version: the shared, read-mostly site the local app pushes market data to (see README, "Web version").
# Standard library only, so the image is just Python + the source. The tests run during the build: an image that
# would leak your own trades or mix up visitors' state is never built.

# --- Stage 1: the downloads on #/download (ahtool/downloads.py): the AH Tool Logger addon and the Windows package
# with the official Windows embeddable Python from python.org, so players need not install anything. Override the
# version with --build-arg PYTHON_EMBED_VERSION=3.13.x and pin it with PYTHON_EMBED_SHA256 (the build then fails on
# any other file). If python.org cannot be reached the package is built without Python and the page says so.
#
# The default is pinned: this is the file players download and run. 3.13.5's sha256 was checked on 23 Nov 2026
# against python.org's own signature (sigstore: `python -m sigstore verify identity --bundle
# python-3.13.5-embed-amd64.zip.sigstore --cert-identity thomas@python.org --cert-oidc-issuer https://accounts.google.com
# python-3.13.5-embed-amd64.zip` -> OK) and the MD5 on the release page. A new version needs a new hash, checked the
# same way - changing only the version fails the build on purpose.
FROM python:3.12-alpine AS downloads
ARG PYTHON_EMBED_VERSION=3.13.5
ARG PYTHON_EMBED_SHA256=7d2650fd9d1b9d002d4a315d5f354247fd6a44f30517c7ef577b08f57a0fb6d9
WORKDIR /src
COPY ahtool ahtool
COPY web web
COPY addon addon
COPY run.py start.bat config.example.json README.md LICENSE ./
RUN python -m ahtool.downloads build /out --python-version "$PYTHON_EMBED_VERSION" \
      ${PYTHON_EMBED_SHA256:+--python-sha256 "$PYTHON_EMBED_SHA256"}

# --- Stage 2: the web server
FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY ahtool ahtool
COPY web web
COPY tests tests
COPY run_web.py ./
COPY --from=downloads /out downloads

RUN mkdir -p data && for t in tests/test_*.py; do python "$t" >/tmp/test.log 2>&1 || { tail -40 /tmp/test.log; exit 1; }; done \
    && rm -rf data tests /tmp/test.log && find / -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null; true

# `docker exec ahtool ahkey create <name>` - sync keys for the people who push data (ahtool/keys.py)
RUN printf '#!/bin/sh\ncd /app && exec python -m ahtool.keys "$@"\n' > /usr/local/bin/ahkey && chmod 755 /usr/local/bin/ahkey

# Run unprivileged. The code stays root-owned (read-only for the server); only /data is writable.
# A volume created by an older, root-running image must be handed over once:
#   docker run --rm -v <volume>:/data alpine chown -R 10001:10001 /data
RUN addgroup -S -g 10001 ahtool && adduser -S -D -H -u 10001 -G ahtool -s /sbin/nologin ahtool \
    && mkdir -p /data && chown ahtool:ahtool /data && chmod 700 /data
USER ahtool:ahtool

EXPOSE 8642
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD wget -qO- http://127.0.0.1:8642/healthz >/dev/null || exit 1
CMD ["python", "run_web.py"]
