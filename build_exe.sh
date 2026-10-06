#!/usr/bin/env bash
# ============================================================
#  Build CNKIDownload single-file binary  (PyInstaller)
#
#  Usage:  ./build_exe.sh [python]
#  Output: dist/CNKIDownload   (macOS/Linux: no .exe suffix)
#
#  The binary is for users who do not want to install Python.
#  Upload it to a GitHub Release - do NOT commit it to the repo.
#  The tool needs no third-party packages, so the binary is small.
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

PY="${1:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY="python"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[FAIL] No python found. Pass the path: ./build_exe.sh /usr/bin/python3" >&2
  exit 1
fi

echo "============================================================"
echo "  CNKIDownload - build single-file binary"
echo "  python : $($PY --version 2>&1)"
echo "  output : $(pwd)/dist/CNKIDownload"
echo "============================================================"
echo

if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
  echo "[1/2] Installing PyInstaller ..."
  "$PY" -m pip install --upgrade pyinstaller
else
  echo "[1/2] PyInstaller already present."
fi

echo
echo "[2/2] Building ..."
"$PY" -m PyInstaller --noconfirm --clean --onefile --console \
  --name CNKIDownload \
  --distpath "$(pwd)/dist" --workpath "$(pwd)/build" --specpath "$(pwd)/build" \
  --add-data "1.check_env.py:." \
  --add-data "2.make_list.py:." \
  --add-data "3.CNKIDownload.py:." \
  --add-data "4.ClearProfile.py:." \
  --hidden-import zipfile \
  --hidden-import xml.etree.ElementTree \
  --hidden-import csv \
  --hidden-import importlib.util \
  --hidden-import argparse \
  --hidden-import base64 \
  --hidden-import platform \
  --hidden-import secrets \
  --hidden-import shutil \
  --hidden-import struct \
  --hidden-import subprocess \
  --hidden-import tempfile \
  --hidden-import urllib.error \
  --hidden-import urllib.parse \
  --hidden-import urllib.request \
  "0.cnki.py"

echo
echo "[OK] Built: $(pwd)/dist/CNKIDownload"
echo
echo "  Verify with:  ./dist/CNKIDownload --check-env"
echo "  Then attach it to a GitHub Release. Do not commit the binary."
