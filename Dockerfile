# Hardened container image for the WARDOGS map vote bot
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --require-hashes --no-deps -r requirements.lock \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin votebot \
 && mkdir -p /data && chown 10001:10001 /data && chmod 700 /data
COPY votebot ./votebot
USER 10001:10001
ENV DATA_DIR=/data
CMD ["python", "-E", "-s", "-m", "votebot"]
