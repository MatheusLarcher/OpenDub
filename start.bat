@echo off
setlocal

set "ROOT=%~dp0"

start "Backend" cmd /k "cd /d %ROOT% && call conda activate AI && python backend\main.py"
start "Frontend" cmd /k "cd /d %ROOT%frontend && npm run dev"

echo Backend e frontend iniciados.
