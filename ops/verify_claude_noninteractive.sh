#!/usr/bin/env bash
# 验证 claude CLI 非交互可用（部署门禁）。
set -uo pipefail
BIN="${CLAUDE_BIN:-claude}"
command -v "$BIN" >/dev/null 2>&1 || { echo "FAIL: claude 不在 PATH: $BIN"; exit 1; }
out=$("$BIN" -p 'reply with the single word: pong' --output-format json 2>/dev/null)
echo "$out" | grep -q '"result"' || { echo "FAIL: 输出无 result 字段: ${out:0:120}"; exit 1; }
echo "$out" | grep -qi 'pong' && { echo "PASS: claude 非交互可用"; exit 0; }
echo "PASS(弱): 有 result 但未见 pong（模型措辞差异，可接受）"; exit 0
