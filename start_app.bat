@echo off
setlocal
cd /d "%~dp0"

rem Use the installed Python runtime even when `streamlit` is not on PATH.
rem Streamlit's CLI includes Unicode characters that the Windows legacy code page cannot print.
set "PYTHONUTF8=1"
set "APP_PYTHON=%LocalAppData%\Python\bin\python.exe"
if not exist "%APP_PYTHON%" set "APP_PYTHON=python"

"%APP_PYTHON%" -m streamlit run streamlit_app.py --server.headless=false
if errorlevel 1 (
  echo.
  echo Streamlit could not start. Install the project requirements, then try again.
  pause
)
