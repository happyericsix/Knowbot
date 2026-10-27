@echo off
chcp 65001 >nul
rem 停止 knowbot daemon：同时结束 python 进程与监护循环（cmd），否则 10 秒后会被拉起
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*knowbot*') -or ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*daemon.cmd*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo knowbot daemon 已停止
