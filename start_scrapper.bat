@echo off
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright"

"%~dp0python\python.exe" -m streamlit run streamlit_app.py --server.address=127.0.0.1 --server.headless=false
