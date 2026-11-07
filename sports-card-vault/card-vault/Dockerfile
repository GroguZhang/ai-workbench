FROM python:3.11-slim
WORKDIR /app

# opencv-python-headless 虽然不带 GUI，但仍动态链接 libglib/libgomp，
# slim 镜像里没有，不装的话 `import cv2` 直接 ImportError。
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

VOLUME ["/app/data", "/app/uploads"]
EXPOSE 8000

# 健康检查用镜像自带的 python，不额外引入 curl（少一个攻击面、少 5MB）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
