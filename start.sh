#!/bin/sh
# Video Crop Tool - start local server (opens browser automatically)
# macOS / Linux: chmod +x start.sh && ./start.sh   (需 python3 + ffmpeg)
cd "$(dirname "$0")"
exec python3 start_server.py
