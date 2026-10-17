#!/bin/sh
# Optional: pip install openai-whisper; install ffmpeg separately.
set -eu
exec python3 "$(dirname "$0")/transcribe.py" "$@"
