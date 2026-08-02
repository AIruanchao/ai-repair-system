FROM python:3.12-slim

WORKDIR /app

# 安装依赖
RUN pip install --no-cache-dir fastapi uvicorn pydantic

# 复制代码
COPY . /app

# 暴露端口
EXPOSE 9100

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:9100/api/health')" || exit 1

# 启动
CMD ["python3", "server.py"]
