#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "========================================"
echo "  DrZ 健身仪表盘 启动中..."
echo "  项目目录（群晖同步）: $(pwd)"
echo "========================================"

LOCAL_DIR="${DRZ_LOCAL_DIR:-$HOME/.drzfitness}"
mkdir -p "$LOCAL_DIR"

if [ ! -d "$LOCAL_DIR/.venv" ]; then
    echo "[!] 首次运行：创建虚拟环境并安装依赖，约 2-3 分钟..."
    python3 -m venv "$LOCAL_DIR/.venv"
    source "$LOCAL_DIR/.venv/bin/activate"
    pip install -r requirements.txt
else
    source "$LOCAL_DIR/.venv/bin/activate"
fi

if [ ! -f "$LOCAL_DIR/.env" ] && [ -f ".env.example" ]; then
    cp .env.example "$LOCAL_DIR/.env"
    echo "[i] 已创建 $LOCAL_DIR/.env，请编辑填入 API Key"
fi

echo ""
echo "[OK] 环境就绪，启动 Streamlit..."
echo "[i] 浏览器访问 http://localhost:8501"
echo ""
streamlit run app.py --server.port 8501
