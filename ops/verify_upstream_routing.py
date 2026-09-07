#!/usr/bin/env python3
"""验证 life-assistant 上游 `_vreader_classify`（应用补丁后）与 vreader 路由契约一致。

被测对象是**上游真实代码**（从 checkout 的 n8n/skill_router.py 动态加载 `_VREADER_*`
正则与 `_vreader_classify` 函数体，非本仓镜像）。附负例自检：删掉 help 分支重跑，
help 契约必须失败——证明测试确实在测上游代码。

用法：
  python ops/verify_upstream_routing.py [life-assistant-checkout]   # 默认 ../life-assistant

前置：checkout 已 `git apply` 补丁 docs/skill_router-vreader-help-detail-confirm.patch
（本脚本不改 checkout；未打补丁则 help/detail 契约会失败并非零退出）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# (输入文本, 期望 endpoint) 契约用例
CASES = [
    ("vr帮助", "/help"),
    ("vr 帮助", "/help"),                                     # vr 与词间空格
    ("/vreader 帮助", "/help"),
    ("vr明细 7681587976547208511", "/detail"),
    ("vr 明细 7681587976547208511", "/detail"),
    ("vr确认 7681587976547208511", "/confirm"),
    ("vr确认 7681587976547208511 a1b2c3d4", "/confirm"),      # 逐条冲突记录码
    ("vr 确认 7681587976547208511 rev:abc123", "/confirm"),   # 绑版本 + 空格
    ("vr榜单", "/board"),
    ("vr 榜单", "/board"),
    ("看看 https://v.douyin.com/abc123/ 复制打开抖音", "/ingest"),
    ("今天天气不错", None),
    ("这个榜单看起来不错啊", None),
]


def _extract_classifier_source(src: str) -> str:
    """从上游源码抽出 module 级 _VREADER_* 赋值 + _vreader_classify 函数体源码段。"""
    tree = ast.parse(src)
    segs = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.startswith("_VREADER") for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name == "_vreader_classify":
            segs.append(ast.get_source_segment(src, node))
    if not any("_vreader_classify" in s for s in segs):
        raise SystemExit("未在上游找到 _vreader_classify（补丁未打或函数改名）")
    return "\n\n".join(segs)


def _load_classifier(source_code: str):
    ns: dict = {"_re": re}
    exec(compile(source_code, "<upstream_vreader>", "exec"), ns)  # noqa: S102 受控源
    return ns["_vreader_classify"]


def _run_cases(classify) -> list[str]:
    fails = []
    for text, want in CASES:
        got = classify(text)
        if got != want:
            fails.append(f"  {text!r}: 期望 {want}, 实得 {got}")
    return fails


def main(argv: list[str]) -> int:
    checkout = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent.parent / "life-assistant"
    src_path = checkout / "n8n" / "skill_router.py"
    if not src_path.exists():
        print(f"找不到上游文件: {src_path}", file=sys.stderr)
        return 2
    src = src_path.read_text(encoding="utf-8")
    classifier_src = _extract_classifier_source(src)

    # 正例：应用补丁后的真实代码须满足全部契约
    fails = _run_cases(_load_classifier(classifier_src))
    if fails:
        print("❌ 上游路由契约不符（补丁是否已 git apply？）：")
        print("\n".join(fails))
        return 1
    print(f"✅ 上游 _vreader_classify 满足全部 {len(CASES)} 条契约")

    # 负例自检：删掉 help 分支 → help 契约必须失败（证明测的是上游代码本身）
    broken = re.sub(r"\n\s*if _VREADER_HELP\.match\(t\):\n\s*return ['\"]/help['\"]",
                    "", classifier_src)
    if broken == classifier_src:
        print("⚠️ 负例自检未能定位 help 分支（正则需随上游写法调整）", file=sys.stderr)
        return 3
    neg_fails = _run_cases(_load_classifier(broken))
    if not any("vr帮助" in f or "帮助" in f for f in neg_fails):
        print("❌ 负例自检失败：删掉 help 分支后 help 契约竟仍通过（测试没在测上游代码）")
        return 4
    print("✅ 负例自检通过：删 help 分支后 help 契约如期失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
