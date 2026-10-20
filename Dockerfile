FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE.md ./
COPY sia ./sia
COPY sia_devtools ./sia_devtools
COPY web ./web
RUN pip install --upgrade pip && pip install ".[server]"
RUN useradd --create-home --uid 10001 sia && mkdir /data && chown sia /data
USER sia
# SIA_TRUSTED_PROXIES: set to your load balancer's address(es) so rate limits see real client IPs
ENV HOST=0.0.0.0 PORT=8000 SIA_DB=/data/sia.db SIA_WEB_DIR=/app/web
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["sia", "serve"]
