#!/bin/bash
# 启动 solar-proxy（后台运行），日志写入 proxy_run.log
cd "$(dirname "$0")"

# 读取本地 .env（UPSTAGE_API_KEY 等环境变量），该文件已被 .gitignore 排除
[ -f .env ] && set -a && source .env && set +a

PORT="${PORT:-8787}"
if lsof -i ":$PORT" >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用（可能已在运行，日志: proxy_run.log）"
  exit 1
fi

if [ -z "$UPSTAGE_API_KEY" ]; then
  echo "错误: 未设置 UPSTAGE_API_KEY。复制 .env.example 为 .env 并填入你的 key。"
  exit 1
fi

nohup python3 -u proxy.py > proxy_run.log 2>&1 &
echo $! > proxy.pid
sleep 1
curl -s "http://127.0.0.1:$PORT/health" && echo && echo "已启动 (PID $(cat proxy.pid))，停止: pkill -f proxy.py"
