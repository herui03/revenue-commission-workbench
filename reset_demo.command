#!/bin/bash
# ---------------------------------------------------------------------------
#  RESET the local demo database (explicit, destructive) - macOS
#  Copies instance/demo_workbench.db to instance/backups/ first, then recreates
#  it with the demo stage-1 data. Stop the running demo (Ctrl+C) before resetting.
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1
DB="instance/demo_workbench.db"
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi
echo "This will REPLACE $DB with fresh demo data (a backup copy is kept in instance/backups/)."
read -r -p "Type RESET and press Enter to continue (anything else cancels): " answer
if [ "$answer" != "RESET" ]; then echo "Cancelled - nothing changed."; read -r -p "Press Enter to close." _; exit 0; fi
"$PY" -m rcw --db "$DB" reset-demo --yes
read -r -p "Done. Press Enter to close." _
