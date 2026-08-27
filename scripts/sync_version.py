#!/usr/bin/env python3
"""把版本号同步到所有需要它的文件。

版本源头是 git 标签；CI 在构建前调用本脚本，避免手工改四处版本导致不一致。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def replace_once(path: pathlib.Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"{path}: 未找到可替换的版本字段")
    path.write_text(updated, encoding="utf-8")


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    if len(argv) != 2 or not SEMVER.match(argv[1]):
        print("用法: sync_version.py <X.Y.Z>", file=sys.stderr)
        return 2
    version = argv[1]

    (ROOT / "jdka/version.py").write_text(
        '"""单一版本源。GitHub Actions 会在打 tag 时把版本同步到这里。"""\n\n'
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    replace_once(ROOT / "pyproject.toml", r'^version = ".*"$', f'version = "{version}"')
    replace_once(ROOT / "package.json", r'^  "version": ".*",$', f'  "version": "{version}",')
    replace_once(
        ROOT / "src-tauri/tauri.conf.json",
        r'^  "version": ".*",$',
        f'  "version": "{version}",',
    )
    replace_once(ROOT / "src-tauri/Cargo.toml", r'^version = ".*"$', f'version = "{version}"')
    print(f"版本已同步为 {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
