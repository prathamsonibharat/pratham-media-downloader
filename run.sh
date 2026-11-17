#!/bin/zsh
# Pratham Instagram Media Downloader — one-click start.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/pip install -q -U yt-dlp >/dev/null 2>&1 &   # keep yt-dlp fresh (Instagram changes often)
(sleep 1.5 && open "http://127.0.0.1:${PORT:-5173}") &
exec .venv/bin/python app.py
