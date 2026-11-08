FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .
EXPOSE 8000

# 单 worker 即可：bundle 缓存是进程内的，多 worker 会各打一轮上游。
# 需要多实例时请在前面加共享缓存或网关限流。
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8000", "--timeout", "120", "app:app"]
