# AI Pulse 运行镜像：Streamlit 页面 + HTTP API 共用同一份代码
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PULSE_DB_PATH=/data/pulse.db \
    PULSE_LOG_DIR=/data/logs

WORKDIR /app

# curl 仅用于容器健康检查
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/logs

EXPOSE 8501 8502

# 默认起页面；API 与调度器在 docker-compose 中覆盖 command
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
