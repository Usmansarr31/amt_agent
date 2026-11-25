#!/usr/bin/env bash
# Lance l'application AMT (Linux / macOS). Usage : ./lancer.sh [options streamlit, ex. --server.port 8502]
# Le secret Direct Line est demandé sans être affiché ; il n'est écrit dans aucun fichier.
set -euo pipefail
cd "$(dirname "$0")"

export AMT_DEFAULT_FILE="${AMT_DEFAULT_FILE:-donnees/DONNEES_03_ATM_ANALYSE_TRAFIC.xlsx}"

if [ -z "${COPILOT_DIRECTLINE_SECRET:-}" ]; then
  read -rsp "Secret Direct Line (Entrée pour lancer sans l'agent) : " COPILOT_DIRECTLINE_SECRET
  echo
fi
if [ -n "$COPILOT_DIRECTLINE_SECRET" ]; then
  export COPILOT_DIRECTLINE_SECRET
else
  unset COPILOT_DIRECTLINE_SECRET
fi

if [ -x .venv/bin/streamlit ]; then
  STREAMLIT=.venv/bin/streamlit
elif [ -x ../.venv/bin/streamlit ]; then
  STREAMLIT=../.venv/bin/streamlit
elif command -v streamlit >/dev/null 2>&1; then
  STREAMLIT=streamlit
else
  echo "Streamlit introuvable : créez l'environnement (python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)." >&2
  exit 1
fi

exec "$STREAMLIT" run app.py "$@"
