@echo off
setlocal
cd /d "%~dp0"

git pull --ff-only origin d-old-version
if errorlevel 1 (
    echo Could not update from GitHub. Check this folder's Git branch and local changes.
    pause
    exit /b 1
)

docker compose up -d --build
if errorlevel 1 (
    echo Docker could not rebuild the app from the updated files.
    pause
    exit /b 1
)

start "" "http://127.0.0.1:8501"
