#!/bin/sh
cd /home/mac/litellm
set -a
. /home/mac/.config/mcp-server/.env
set +a
exec .venv/bin/litellm --config config.yaml --host 127.0.0.1 --port 4000
