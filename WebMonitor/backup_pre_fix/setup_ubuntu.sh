#!/usr/bin/env bash
# ==============================================================================
# AntMinerTab - Ubuntu 22.04 LTS One-Click Setup Script
# ==============================================================================

set -e

echo "=========================================================="
echo " 🚀 AntMinerTab: Initializing setup for Ubuntu 22.04 LTS"
echo "=========================================================="

# 1. Update APT repository
echo "[1/4] Updating APT package repositories..."
sudo apt update -y

# 2. Install Python 3, PyQt5, PyQtGraph, Cryptography & Requests via APT
# Using native Ubuntu APT packages avoids wheel compilation issues
echo "[2/4] Installing system dependencies and Python packages..."
sudo apt install -y \
    python3 \
    python3-pip \
    python3-requests \
    python3-cryptography \
    python3-pyqt5 \
    python3-pyqtgraph \
    fonts-dejavu \
    fonts-ubuntu

# 3. Grant execute permissions to scripts
echo "[3/4] Granting execution permissions..."
chmod +x antminer_gui.py antminer_monitor.py antminer_web.py

# 4. Verification
echo "[4/4] Verifying Python module availability..."
python3 -c "
import requests
import cryptography
import PyQt5
import pyqtgraph
print('  ✓ requests: OK')
print('  ✓ cryptography: OK')
print('  ✓ PyQt5: OK')
print('  ✓ pyqtgraph: OK')
"

echo "=========================================================="
echo " ✅ Installation Successful!"
echo ""
echo " To run Desktop GUI Monitor:"
echo "   python3 antminer_gui.py"
echo ""
echo " To run Headless Terminal / Server Monitor (with auto-protection & Telegram):"
echo "   python3 antminer_monitor.py --watch"
echo "=========================================================="
