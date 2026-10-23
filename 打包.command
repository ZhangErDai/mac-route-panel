#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
export PYINSTALLER_CONFIG_DIR="$PWD/build/.pyinstaller"
exec .venv/bin/python -m PyInstaller --noconfirm --clean RoutePanel.spec
