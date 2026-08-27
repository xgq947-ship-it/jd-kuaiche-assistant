#!/usr/bin/env bash
# 把 Python 后端打成独立可执行文件，供 Tauri 作为 resource 打包。
# 产物：src-tauri/resources/backend/jdka-backend[.exe]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

python="${PYTHON:-}"
if [ -z "$python" ]; then
  if   [ -x ".venv/bin/python" ];        then python=".venv/bin/python"
  elif [ -x ".venv/Scripts/python.exe" ]; then python=".venv/Scripts/python.exe"
  else python="python3"; fi
fi

echo "==> 使用解释器：$python"
"$python" -m pip install --quiet --upgrade pyinstaller >/dev/null 2>&1 || true

out="src-tauri/resources/backend"
rm -rf build dist "$out"
mkdir -p "$(dirname "$out")"

# onedir：macOS 上单文件窗口程序每次启动都要解包，且不利于签名与公证。
"$python" -m PyInstaller --noconfirm --clean \
  --name jdka-backend \
  --onedir --console \
  --distpath dist \
  --add-data "jdka/ui:jdka/ui" \
  --collect-all playwright \
  --hidden-import jdka.server \
  --hidden-import jdka.service \
  jdka/cli.py

mv "dist/jdka-backend" "$out"
echo "==> 后端已就绪：$out"
