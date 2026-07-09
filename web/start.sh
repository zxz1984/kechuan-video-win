#!/bin/bash
# 可乐 AI 分析 网页版 启动脚本
# 用法：./web/start.sh [prod|dev]
#   dev  = Flask 内置服务器（开发用，单线程）
#   prod = gunicorn 多 worker（推荐自用）

set -e
cd "$(dirname "$0")/.."

MODE="${1:-prod}"
PORT="${WEB_PORT:-8080}"

# 1) 检查 Python 3.13
if ! command -v python3.13 &> /dev/null; then
  echo "❌ python3.13 未安装"
  echo "   brew install python@3.13"
  exit 1
fi

# 2) 检查 ffmpeg
if ! command -v ffmpeg &> /dev/null; then
  echo "❌ ffmpeg 未安装"
  echo "   brew install ffmpeg   # Mac"
  echo "   sudo apt install -y ffmpeg   # Ubuntu/Debian"
  exit 1
fi

# 3) 装依赖
echo "📦 装依赖..."
python3.13 -m pip install --user -q -r web/requirements-web.txt 2>&1 | tail -3

# 4) 启动
if [ "$MODE" = "dev" ]; then
  echo "🚀 开发模式启动: http://0.0.0.0:$PORT"
  exec python3.13 -m web.server
else
  echo "🚀 生产模式启动 (gunicorn): http://0.0.0.0:$PORT"
  exec python3.13 -m gunicorn \
    --workers 2 \
    --threads 2 \
    --bind "0.0.0.0:$PORT" \
    --timeout 600 \
    --access-logfile /tmp/web_access.log \
    --error-logfile /tmp/web_error.log \
    web.server:app
fi