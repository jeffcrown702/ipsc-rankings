@echo off
REM IPSC Rankings - Server Launcher
REM 自動啟動 FastAPI server + Serveo tunnel

cd /d E:\ctb988\ipsc-rankings

:TUNNEL
echo [%date% %time%] Starting Serveo tunnel...
start /B "" ssh -o StrictHostKeyChecking=no -R 80:localhost:8010 serveo.net

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
