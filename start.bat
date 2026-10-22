@echo off
rem Double-click to start; this wrapper calls start.ps1 with ExecutionPolicy Bypass.
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
echo.
echo 服务已退出，按任意键关闭窗口。
pause >nul
