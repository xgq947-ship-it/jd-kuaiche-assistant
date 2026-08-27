#!/usr/bin/env bash
# 把 Python 后端打成独立可执行文件，供 Tauri 作为 resource 打包。
# 产物：src-tauri/resources/backend/jdka-backend[.exe]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# Windows 的 Git Bash 通常只有 python，没有 python3。
python="${PYTHON:-}"
if [ -z "$python" ]; then
  if   [ -x ".venv/bin/python" ];         then python=".venv/bin/python"
  elif [ -x ".venv/Scripts/python.exe" ]; then python=".venv/Scripts/python.exe"
  elif command -v python3 >/dev/null 2>&1; then python="python3"
  else python="python"; fi
fi

# --add-data 的分隔符随平台变化：Windows 是 ';'，其余是 ':'。写死会在另一端失败。
sep="$("$python" -c 'import os; print(os.pathsep)')"

echo "==> 解释器：$python"
echo "==> add-data 分隔符：$sep"

"$python" -m PyInstaller --version >/dev/null 2>&1 || {
  echo "未检测到 PyInstaller，请先执行：$python -m pip install pyinstaller" >&2
  exit 1
}

out="src-tauri/resources/backend"
rm -rf build dist "$out"
mkdir -p "$(dirname "$out")"

# onedir：macOS 上单文件窗口程序每次启动都要解包，且不利于签名与公证。
"$python" -m PyInstaller --noconfirm --clean \
  --name jdka-backend \
  --onedir --console \
  --distpath dist \
  --add-data "jdka/ui${sep}jdka/ui" \
  --collect-all playwright \
  --hidden-import jdka.server \
  --hidden-import jdka.service \
  --hidden-import jdka.license \
  jdka/cli.py

mv "dist/jdka-backend" "$out"
echo "==> 后端已就绪：$out"
