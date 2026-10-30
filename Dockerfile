# AI Pulse 运行镜像：Streamlit 页面 + HTTP API 共用同一份代码
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HOME=/home/pulse \
    PULSE_DB_PATH=/data/pulse.db \
    PULSE_LOG_DIR=/data/logs \
    PULSE_CHROMA_DIR=/data/chroma

WORKDIR /app

# curl 用于容器健康检查；tzdata 保证容器内日期/时间与北京时间一致
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 非 root 运行：数据目录与代码目录都交给 pulse 用户，降低容器逃逸风险
RUN useradd --create-home --uid 10001 pulse \
    && mkdir -p /data/logs /data/chroma \
    && chown -R pulse:pulse /app /data /home/pulse

USER pulse

EXPOSE 8501 8502

# 默认起页面；API 与调度器在 docker-compose 中覆盖 command
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
