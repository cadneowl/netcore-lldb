#!/bin/bash
# Build the reference image (used as the docker-exec target in run_all.py).
set -e
cd "$(dirname "$0")/.."
LOG=/tmp/netcore-lldb-build.log
echo "build context: ./reference-image" | tee "$LOG"
docker build -t netcore-lldb:dev ./reference-image 2>&1 | tee -a "$LOG"
echo
echo "=== final image ==="
docker images netcore-lldb:dev
