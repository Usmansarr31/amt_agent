@echo off
rem Lance l'application AMT (Windows). Double-cliquer, ou : lancer.bat [options streamlit]
rem Le secret Direct Line est demande sans etre affiche ; il n'est ecrit dans aucun fichier.
setlocal
cd /d "%~dp0"

if not defined AMT_DEFAULT_FILE set "AMT_DEFAULT_FILE=donnees\DONNEES_03_ATM_ANALYSE_TRAFIC.xlsx"

if not defined COPILOT_DIRECTLINE_SECRET (
  for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "$s = Read-Host 'Secret Direct Line, Entree pour lancer sans agent' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "COPILOT_DIRECTLINE_SECRET=%%s"
)

set "STREAMLIT="
if exist ".venv\Scripts\streamlit.exe" set "STREAMLIT=.venv\Scripts\streamlit.exe"
if not defined STREAMLIT if exist "..\.venv\Scripts\streamlit.exe" set "STREAMLIT=..\.venv\Scripts\streamlit.exe"
if not defined STREAMLIT where streamlit >nul 2>nul && set "STREAMLIT=streamlit"
if not defined STREAMLIT (
  echo Streamlit introuvable : creez l'environnement avec "py -m venv .venv" puis ".venv\Scripts\pip install -r requirements.txt".
  pause
  exit /b 1
)

"%STREAMLIT%" run app.py %*
if errorlevel 1 pause
endlocal
