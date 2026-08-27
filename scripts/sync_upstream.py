#!/usr/bin/env python3
"""与上游 ecommerce-store-ops 对账 / 同步。

本项目是从 `ecommerce-store-ops` 复制出来的独立产品，**不修改上游一行代码**。
代价是两边会分叉：上游修了 bug，这边不会自动拿到。

这个脚本把「抽取时做过哪些适配」显式声明出来，于是可以做**归一化对比**：
把上游文件套上同样的适配后再比，剩下的差异就是真正的漂移。

    python scripts/sync_upstream.py            # 只对账，报告差异
    python scripts/sync_upstream.py --pull     # 把上游改动同步过来（会覆盖本地）
    python scripts/sync_upstream.py --diff     # 连同具体 diff 一起打印

**不覆盖的东西**：本项目自有的文件（browser/、engine.py、license.py、server.py …）
不在对账范围内，上游没有它们。
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
UPSTREAM_ENV = "JDKA_UPSTREAM"
DEFAULT_UPSTREAM = pathlib.Path.home() / "Desktop/电商Brain/02-运营店铺"

# 抽取时对上游文件做过的适配。对账时先把这些套到上游内容上再比，
# 因此这些差异不会被误报成漂移；新增适配必须登记在这里，否则会一直报差异。
ADAPTATIONS: dict[str, list[tuple[str, str]]] = {
    "jdka/jd/shared.py": [
        ("# 该结论由 9222 页面实测确定：", "# 该结论由真实页面实测确定："),
        (
            '"请在 9222 Chrome Beta 登录京准通并打开快车计划页后重试。"',
            '"京准通登录态已失效，请在应用内点击「登录京准通」重新登录。"',
        ),
        (
            '"请刷新 9222 京准通快车页，使页面重新安装 h5st 签名钩子。"',
            '"后台页面需要重新加载以安装 h5st 签名钩子，将自动重试。"',
        ),
    ],
    "jdka/jd/report.py": [
        ("from ops_cli.platforms.jd.shared import", "from jdka.jd.shared import"),
    ],
    "jdka/jd/plan.py": [
        (
            "from ops_cli.platforms.jd.kuaiche_report import",
            "from jdka.jd.report import",
        ),
        ("from ops_cli.platforms.jd.shared import", "from jdka.jd.shared import"),
    ],
}

# 本地路径 -> 上游路径
PAIRS: dict[str, str] = {
    "jdka/jd/shared.py": "Ops-Cli/src/ops_cli/platforms/jd/shared.py",
    "jdka/jd/report.py": "Ops-Cli/src/ops_cli/platforms/jd/kuaiche_report.py",
    "jdka/jd/plan.py": "Ops-Cli/src/ops_cli/platforms/jd/kuaiche_plan.py",
    "jdka/core/naming.py": "运营自动化工具/workflows/jd_kuaiche_plan_rotate/naming.py",
    "jdka/core/state.py": "运营自动化工具/workflows/jd_kuaiche_plan_rotate/state_store.py",
    "jdka/core/policy.py": "运营自动化工具/workflows/jd_kuaiche_plan_rotate/rotate_policy.py",
}


def adapt(local_rel: str, upstream_text: str) -> str:
    text = upstream_text
    for needle, replacement in ADAPTATIONS.get(local_rel, []):
        if needle not in text:
            print(
                f"  ⚠️  {local_rel}: 适配规则已失效（上游找不到「{needle[:40]}…」），"
                "上游可能改写了这段，请人工确认后更新 ADAPTATIONS",
                file=sys.stderr,
            )
        text = text.replace(needle, replacement)
    return text


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(description="与上游 ecommerce-store-ops 对账/同步")
    parser.add_argument("--pull", action="store_true", help="把上游改动同步到本地（覆盖）")
    parser.add_argument("--diff", action="store_true", help="打印具体差异")
    parser.add_argument(
        "--upstream",
        type=pathlib.Path,
        default=pathlib.Path(DEFAULT_UPSTREAM),
        help=f"上游仓库路径（默认 {DEFAULT_UPSTREAM}，也可用 {UPSTREAM_ENV} 环境变量）",
    )
    args = parser.parse_args()

    import os

    upstream = pathlib.Path(os.environ.get(UPSTREAM_ENV) or args.upstream).expanduser()
    if not upstream.is_dir():
        print(f"找不到上游仓库：{upstream}", file=sys.stderr)
        return 1

    drifted: list[str] = []
    missing: list[str] = []

    for local_rel, upstream_rel in PAIRS.items():
        local_path = ROOT / local_rel
        upstream_path = upstream / upstream_rel
        if not upstream_path.exists():
            missing.append(f"{local_rel}  ←  上游已不存在 {upstream_rel}")
            continue

        expected = adapt(local_rel, upstream_path.read_text(encoding="utf-8"))
        actual = local_path.read_text(encoding="utf-8") if local_path.exists() else ""

        if expected == actual:
            print(f"  ✅ {local_rel}")
            continue

        drifted.append(local_rel)
        added = sum(
            1
            for line in difflib.unified_diff(actual.splitlines(), expected.splitlines())
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        print(f"  ⚠️  {local_rel}  差异 {added} 行")
        if args.diff:
            for line in difflib.unified_diff(
                actual.splitlines(),
                expected.splitlines(),
                fromfile=f"本地 {local_rel}",
                tofile=f"上游 {upstream_rel}(已适配)",
                lineterm="",
            ):
                print("     " + line)
        if args.pull:
            local_path.write_text(expected, encoding="utf-8")
            print(f"     → 已同步")

    print()
    for line in missing:
        print(f"  ❌ {line}")
    if not drifted and not missing:
        print("与上游一致。")
        return 0
    if args.pull:
        print(f"已同步 {len(drifted)} 个文件。**务必跑一遍测试**：python -m pytest -q")
        return 0
    print(f"有 {len(drifted)} 个文件与上游不一致。看差异加 --diff，同步加 --pull。")
    return 1 if drifted or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
