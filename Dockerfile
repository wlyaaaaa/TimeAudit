# 使用轻量级 Python 基础镜像
FROM python:3.10-slim

# ⚡ 核心操作：在构建镜像时就把需要的环境装好，以后开机无需再联网下载！
RUN apt-get update && \
    apt-get install -y libpq5 && \
    pip install psycopg && \
    # 清理缓存，减小镜像体积
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 开机直接运行 Python 脚本
CMD ["python", "-u", "ingest.py"]