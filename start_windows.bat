@echo off
REM J.A.R.V.I.S. OS - quick start on Windows
cd /d "%~dp0"
if not exist .venv (
  echo [JARVIS] Creating the virtual environment...
  python -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || goto :err
if not exist .env (
  copy .env.example .env >nul
  echo [JARVIS] Created .env: add ANTHROPIC_API_KEY and restart.
  notepad .env
)
python run.py %*
goto :eof
:err
echo [JARVIS] Installation failed.
pause
