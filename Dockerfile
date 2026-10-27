FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 通过挂载 config.yaml 提供真实配置；未挂载时回退到 config.example.yaml（空 Key 仅告警）
ENV POLYGATE_PORT=8000

EXPOSE 8000

CMD ["python", "run.py"]
