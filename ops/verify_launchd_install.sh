#!/usr/bin/env bash
# 验证 launchd 安装与健康（部署门禁）。在生产机上运行。
set -uo pipefail
PLIST="$HOME/Library/LaunchAgents/ai.chivox.vreader.plist"
PORT="${VREADER_PORT:-8232}"
fail=0

[ -f "$PLIST" ] || { echo "FAIL: 缺 plist $PLIST"; fail=1; }
if [ -f "$PLIST" ]; then
  grep -q "/.venv/bin/python" "$PLIST" || { echo "FAIL: plist 未用绝对解释器"; fail=1; }
  grep -q "/usr/local/bin" "$PLIST" || { echo "FAIL: plist 未显式 PATH"; fail=1; }
  grep -q "KeepAlive" "$PLIST" || { echo "FAIL: plist 缺 KeepAlive"; fail=1; }
fi

# .env 权限 0600
ENVF="$(dirname "$0")/../.env"
if [ -f "$ENVF" ]; then
  perm=$(stat -f "%Lp" "$ENVF" 2>/dev/null || stat -c "%a" "$ENVF" 2>/dev/null)
  [ "$perm" = "600" ] || { echo "FAIL: .env 权限 $perm != 600"; fail=1; }
else
  echo "WARN: 未找到 .env"
fi

# healthz
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$PORT/healthz" -d '{}' 2>/dev/null)
[ "$code" = "200" ] || { echo "FAIL: healthz 非 200（=$code）"; fail=1; }

[ "$fail" = 0 ] && { echo "PASS: launchd 安装与健康检查通过"; exit 0; }
echo "launchd 校验未通过"; exit 1
