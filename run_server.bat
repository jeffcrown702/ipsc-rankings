@echo off
REM IPSC Rankings - Server Launcher
REM 自動啟動 FastAPI server + localhost.run tunnel

cd /d E:\ctb988\ipsc-rankings

:TUNNEL
echo [%date% %time%] Starting localhost.run tunnel...
start /B "" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 -o ServerAliveInterval=15 -R 80:localhost:8010 nokey@localhost.run

echo [%date% %time%] Starting FastAPI server...
python -u -c "
import sys; sys.path.insert(0, '.')
import uvicorn
from app import app
uvicorn.run(app, host='0.0.0.0', port=8010, log_level='info')
"

echo Server exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto TUNNEL
