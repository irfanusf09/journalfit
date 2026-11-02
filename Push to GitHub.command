#!/bin/bash
set -e
SRC="/Users/irfanyousf/Desktop/journalfit-main"
cd "$SRC"

echo "1. Fetching latest history from github.com/irfanusf09/journalfit..."
git fetch origin main
git branch -M main
git reset --soft origin/main
git add -A
if ! git diff --cached --quiet; then
  git commit -m "Fix Chrome extension Autopilot toggle, service worker auto-reload, and two-phase form filling"
fi

echo "2. Pushing updates to GitHub (origin/main)..."
git push -u origin main

if [ -d "/Users/irfanyousf/journalfit" ]; then
  echo "3. Syncing your local /Users/irfanyousf/journalfit folder..."
  rsync -a --exclude='.git' "$SRC/" "/Users/irfanyousf/journalfit/"
fi

echo ""
echo "✓ All updates have been pushed to GitHub and synced on your Mac!"
