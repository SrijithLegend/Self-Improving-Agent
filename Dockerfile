FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sia ./sia
ENV HOST=0.0.0.0 PORT=8000 SIA_DB=/data/sia.db
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "sia", "serve"]
