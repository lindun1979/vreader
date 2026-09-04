#!/usr/bin/env bash
# M0 前提证伪：抖音分享链接能否程序化下载（无登录）。
# 读 tests/fixtures/douyin_links.private.txt（每行一条分享文本/链接，gitignore）。
# 每条连续两次下载、ffprobe 可解码即通过；记录 aweme_id 与时长。
set -uo pipefail
cd "$(dirname "$0")/.."
LINKS="tests/fixtures/douyin_links.private.txt"
PY="${VREADER_PY:-.venv/bin/python}"
[ -f "$LINKS" ] || { echo "缺 $LINKS（每行一条真实分享链接）"; exit 1; }

pass=0; total=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  total=$((total+1))
  echo "=== [$total] $line"
  ok=1
  for attempt in 1 2; do
    out=$("$PY" - "$line" <<'PY'
import sys, tempfile, os, subprocess
sys.path.insert(0, ".")
from core import douyin
text = sys.argv[1]
try:
    aid = douyin.resolve_aweme_id(text)
    d = tempfile.mkdtemp()
    dest = os.path.join(d, aid + ".mp4")
    meta = douyin.download(aid, dest)
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0", dest], capture_output=True, text=True)
    dur = r.stdout.strip()
    print(f"OK aid={aid} bytes={os.path.getsize(dest)} probe_dur={dur}")
except Exception as e:
    print(f"FAIL {e}")
PY
)
    echo "  attempt $attempt: $out"
    case "$out" in OK*) ;; *) ok=0 ;; esac
  done
  [ "$ok" = 1 ] && pass=$((pass+1))
done < "$LINKS"

echo "=== 通过 $pass/$total"
[ "$pass" = "$total" ] && [ "$total" -ge 3 ] && { echo "M0 PASS"; exit 0; }
echo "M0 未达标（需 >=3 条全部两次成功）"; exit 1
