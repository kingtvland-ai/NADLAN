#!/usr/bin/env bash
# scripts/bootstrap.sh
set -euo pipefail

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dev]'
playwright install chromium
