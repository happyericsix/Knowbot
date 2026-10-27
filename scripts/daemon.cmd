@echo off
rem knowbot daemon 监护脚本：daemon 退出后等 10 秒自动重启。
rem 工作目录固定为仓库根，保证相对路径 ./data/knowbot.db 稳定。
cd /d E:\knowbot
:loop
.venv\Scripts\python.exe -m knowbot daemon >> data\daemon.log 2>&1
timeout /t 10 /nobreak >nul
goto loop
