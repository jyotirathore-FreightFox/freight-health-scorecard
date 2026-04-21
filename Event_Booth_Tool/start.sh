#!/bin/bash
# FreightFox Freight Health Scorecard — Launcher
# Run this script to start the backend and open the scorecard in a browser.

cd "$(dirname "$0")"

# Check for virtual environment
if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Install Playwright browsers (only chromium)
python -m playwright install chromium 2>/dev/null || echo "Playwright browsers already installed"

# Start server
echo ""
echo "============================================"
echo "  FreightFox Freight Health Scorecard"
echo "  Running at http://localhost:8000"
echo "  Press Ctrl+C to stop"
echo "============================================"
echo ""

# Open browser after a short delay
(sleep 2 && open http://localhost:8000) &

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
