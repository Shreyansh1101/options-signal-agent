#!/bin/bash
# Runs every 15 minutes: pulls changes from GitHub (code or config.yaml edits) and restarts the agent if anything changed.
set -e
cd /opt/signal-agent
git fetch -q origin
if [ "$(git rev-parse HEAD)" != "$(git rev-parse '@{u}')" ]; then
  git reset -q --hard '@{u}'
  .venv/bin/pip install -q -r requirements.txt
  systemctl restart signal-agent
  echo "$(date -Is) updated to $(git rev-parse --short HEAD)"
fi
