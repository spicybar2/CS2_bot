# 构建: docker build -t cs2-qq-bot .
# 运行: docker run -d --name cs2-qq-bot --restart=always \
#        -v /opt/cs2-qq-bot/config.yaml:/app/config.yaml:ro \
#        -v /opt/cs2-qq-bot/data:/app/data \
#        -v /opt/cs2-qq-bot/logs:/app/logs \
#        cs2-qq-bot

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据与日志目录
RUN mkdir -p /app/data /app/logs

CMD ["python", "main.py"]
