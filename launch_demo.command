#!/bin/bash
# ---------------------------------------------------------------------------
#  Revenue & Commission Operations Workbench - start the local demo (macOS)
#  Double-click this file in Finder. It:
#    * starts the web UI on http://127.0.0.1:5057 (this computer only)
#    * KEEPS your existing demo database (instance/demo_workbench.db);
#      it only creates + seeds one if that file does not exist yet
#    * never installs packages or changes system settings
#  Stop the demo: press Ctrl+C in this window (or close the window).
#  Reset is separate and explicit: reset_demo.command
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1
PORT=5057
DB="instance/demo_workbench.db"

pause_exit() { echo; read -r -p "Press Enter to close this window." _; exit "${1:-1}"; }

# 1. Which Python? Prefer the project's virtual environment if you created one.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "Python 3 was not found."
  echo "Install Python 3.10 or newer from https://www.python.org/downloads/macos/ and double-click this file again."
  pause_exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required; found: $("$PY" --version 2>&1)"
  echo "Install a newer Python from https://www.python.org/downloads/macos/"
  pause_exit 1
fi

# 2. Is Flask (the web UI library) installed? We do not install it for you.
if ! "$PY" -c 'import flask' >/dev/null 2>&1; then
  echo "The web UI needs Flask, which is not installed for $PY."
  echo
  echo "Run these two commands once in Terminal (copy and paste), then double-click this file again:"
  echo "  cd \"$(pwd)\""
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo
  echo "Without Flask you can still run the scripted demo (no web UI):  python3 -m rcw demo"
  pause_exit 1
fi

# 3. Port free?
if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use - the demo may already be running."
  echo "Open http://127.0.0.1:$PORT in your browser, or close the other window first."
  pause_exit 1
fi

if [ -f "$DB" ]; then
  echo "Using your existing demo database: $DB (nothing is reset)."
else
  echo "First run: creating $DB with the synthetic demo data (stage 1)."
fi
echo "Opening http://127.0.0.1:$PORT ... (press Ctrl+C here to stop)"
( sleep 2; command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT" ) &
"$PY" -m rcw --db "$DB" serve --seed-if-missing --port "$PORT"
pause_exit $?
