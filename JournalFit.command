#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/pyproject.toml" ]; then
  cd "$DIR"
elif [ -d "/Users/irfanyousf/Desktop/journalfit-main" ]; then
  cd "/Users/irfanyousf/Desktop/journalfit-main"
else
  cd "/Users/irfanyousf/journalfit"
fi
# Stop any older running JournalFit server so the new version always gets port 8765
pkill -f "journalfit.app" 2>/dev/null || true
sleep 0.3
"/Library/Frameworks/Python.framework/Versions/3.15/bin/python3" -m journalfit.app
