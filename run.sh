#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

# Headless Chromium for the animation templates (no-op once installed).
python -m playwright install chromium

if [ ! -f .env ]; then
  echo "⚠️  No .env found — copy .env.example to .env and add your keys."
  cp .env.example .env
fi

# video-studio uses 5050; Dmiqwi defaults to 5066 so fleet workers keep a
# stable controller address across restarts.
# (NOT 5060/5061 — Chrome blocks those as unsafe SIP ports).
export PORT="${PORT:-5066}"
echo ""
echo "  ▶  Dmiqwi  →  http://127.0.0.1:${PORT}   (keep this terminal open)"
echo ""
if [ "$(uname -s)" = "Darwin" ] && command -v caffeinate >/dev/null 2>&1; then
  # Multi-hour renders must not pause because the MacBook enters idle sleep.
  # Keep the system and disks awake while the controller is alive without
  # forcing the display to remain on.
  exec caffeinate -im python app.py
fi
exec python app.py
