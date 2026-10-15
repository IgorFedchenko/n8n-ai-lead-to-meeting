#!/bin/sh
# Convert an actual passing Chromium capture, never a rendered mock screenshot.
set -eu
if [ "$#" -ne 1 ]; then
  echo 'usage: scripts/encode_demo.sh artifacts/video/ACTUAL_CAPTURE.webm' >&2
  exit 2
fi
ffmpeg -v error -threads 1 -i "$1" -vf 'fps=6,scale=880:-1:flags=lanczos' -loop 0 -y docs/images/demo.gif
