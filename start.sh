#!/usr/bin/env bash
set -e

echo "================================================================="
echo "  ＧＡＭＥＲ-ＣＯＲＰＯＲＡＴＩＯＮ - Auto Setup & Start Script (Cloud Shell / Linux)"
echo "================================================================="

echo "[*] Installing Python packages..."
python3 -m pip install -q -r requirements.txt || python3 -m pip install -q --user --break-system-packages -r requirements.txt

echo "[+] Starting ＧＡＭＥＲ-ＣＯＲＰ Dashboard..."
exec python3 run.py
