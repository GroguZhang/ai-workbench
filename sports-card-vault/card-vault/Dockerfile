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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
