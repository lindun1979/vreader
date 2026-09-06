#!/usr/bin/env bash
# 生产运维入口（在生产机 Mac Mini 上运行）。用法：ops/prod.sh {health|restart|logs|board}
# 内网细节见 DEPLOY.local.md（gitignore）。
set -uo pipefail
PORT="${VREADER_PORT:-8232}"
LABEL="ai.chivox.vreader"

case "${1:-}" in
  health)
    # 双重判定（C5）：curl 退出码 与 HTTP 状态码都必须 OK，不能被 echo 覆盖退出码。
    # 输出 body + 单独一行 HTTP 状态；退出码：curl 非 0 → 该码；HTTP≠200 → 1。
    body="$(curl -s -m 10 -w '\n__HTTP__%{http_code}' -X POST \
              "http://127.0.0.1:$PORT/healthz" -d '{}')"
    rc=$?
    if [ $rc -ne 0 ]; then
      echo "FAIL curl exit=$rc"; exit $rc
    fi
    code="${body##*__HTTP__}"
    echo "${body%__HTTP__*}"
    echo "HTTP $code"
    [ "$code" = "200" ] || { echo "FAIL http=$code"; exit 1; }
    ;;
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
