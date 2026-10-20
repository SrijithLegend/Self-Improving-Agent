FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sia ./sia
COPY sia_devtools ./sia_devtools
RUN pip install --no-cache-dir ".[server]"
RUN useradd --create-home sia && mkdir /data && chown sia /data
USER sia
ENV HOST=0.0.0.0 PORT=8000 SIA_DB=/data/sia.db
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["sia", "serve"]
