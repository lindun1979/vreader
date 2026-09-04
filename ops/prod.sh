#!/usr/bin/env bash
# 生产运维入口（在生产机 Mac Mini 上运行）。用法：ops/prod.sh {health|restart|logs|board}
# 内网细节见 DEPLOY.local.md（gitignore）。
set -uo pipefail
PORT="${VREADER_PORT:-8232}"
LABEL="ai.chivox.vreader"

case "${1:-}" in
  health)
    curl -s -X POST "http://127.0.0.1:$PORT/healthz" -d '{}'; echo ;;
  restart)
    launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null
    launchctl load "$HOME/Library/LaunchAgents/$LABEL.plist"
    echo "reloaded $LABEL" ;;
  logs)
    tail -n 50 /tmp/vreader.err.log /tmp/vreader.out.log ;;
  board)
    cat "$(dirname "$0")/../data/token_bug/board.md" 2>/dev/null || echo "无榜单" ;;
  *)
    echo "用法: ops/prod.sh {health|restart|logs|board}"; exit 1 ;;
esac
