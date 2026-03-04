#!/bin/bash
# Start the webhook server (and optionally expose via ngrok)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${WEBHOOK_PORT:-8000}"

# Ensure dependencies are installed
pip3 install -q -r "$SCRIPT_DIR/requirements.txt"

echo "Starting Technical Writer webhook server on port $PORT..."
echo "Health check: http://localhost:$PORT/health"
echo ""
echo "To expose publicly with ngrok (in a separate terminal):"
echo "  ngrok http $PORT"
echo "Then set GitHub webhook URL to: https://<ngrok-id>.ngrok.io/webhook"
echo ""

cd "$SCRIPT_DIR"
python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload
