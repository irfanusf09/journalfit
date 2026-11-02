#!/bin/bash
set -e
SRC="/Users/irfanyousf/Desktop/journalfit-main"
cd "$SRC"

# Copy latest form_report.txt from ~/Documents/JournalFit if present so we can inspect any unrecognised portal boxes
LATEST_REPORT=$(ls -t "$HOME"/Documents/JournalFit/*/*/form_report.txt 2>/dev/null | head -n 1 || true)
if [ -n "$LATEST_REPORT" ] && [ -f "$LATEST_REPORT" ]; then
  cp "$LATEST_REPORT" "$SRC/.last_form_report.txt"
fi

# Fix any already-saved Taylor & Francis submission URLs in ~/Documents/JournalFit that end with "20"
"/Library/Frameworks/Python.framework/Versions/3.15/bin/python3" - <<'PY'
import glob, json, os, re
root = os.path.expanduser("~/Documents/JournalFit")
rx = re.compile(r"(https?://rp\.tandfonline\.com/submission/create\?journalCode=)([a-zA-Z]+)\d*", re.I)
for path in glob.glob(os.path.join(root, "*", "*", "metadata.json")):
    try:
        data = json.load(open(path, encoding="utf-8"))
        u = data.get("submission_url") or ""
        new_u = rx.sub(lambda m: m.group(1) + m.group(2).upper(), u)
        if new_u != u:
            data["submission_url"] = new_u
            json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass
for path in glob.glob(os.path.join(root, "*", "*", "checklist.md")):
    try:
        txt = open(path, encoding="utf-8").read()
        new_txt = rx.sub(lambda m: m.group(1) + m.group(2).upper(), txt)
        if new_txt != txt:
            open(path, "w", encoding="utf-8").write(new_txt)
    except Exception:
        pass
PY

echo "1. Fetching latest history from github.com/irfanusf09/journalfit..."
git fetch origin main
git branch -M main
git reset --soft origin/main
git add -A
if ! git diff --cached --quiet; then
  git commit -m "Fix Taylor & Francis journalCode URL suffix and form field matching"
fi

echo "2. Pushing updates to GitHub (origin/main)..."
git push -u origin main

if [ -d "/Users/irfanyousf/journalfit" ]; then
  echo "3. Syncing your local /Users/irfanyousf/journalfit folder..."
  rsync -a --exclude='.git' "$SRC/" "/Users/irfanyousf/journalfit/"
fi

echo "4. Restarting local JournalFit server on port 8765..."
pkill -f "journalfit.app" 2>/dev/null || true
nohup "/Library/Frameworks/Python.framework/Versions/3.15/bin/python3" -m journalfit.app --no-browser >/dev/null 2>&1 &

echo ""
echo "✓ All updates pushed to GitHub, synced, and JournalFit server restarted!"
