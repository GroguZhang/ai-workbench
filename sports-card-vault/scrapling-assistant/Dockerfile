FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 默认走官方源；国内构建时用 --build-arg PIP_INDEX_URL=<镜像源> 覆盖，
# 避免把某个地区的镜像源硬编码进镜像（换环境就拉不到包）。
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i "${PIP_INDEX_URL}"

RUN scrapling install

COPY app.py .

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["curl", "-fsS", "-m", "4", "http://127.0.0.1:8090/health"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]

