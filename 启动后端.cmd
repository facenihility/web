@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
  set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)
if not defined PY (
  echo [错误] 未找到 python，请先安装 Python 3.8+ 或将其加入 PATH。
  pause
  exit /b 1
)

echo ============================================================
echo  工业智算网 · 通算协同调度平台  后端服务
echo ============================================================
echo   前端页面   http://127.0.0.1:8787/
echo   硬件接口   http://127.0.0.1:8787/api/hw/schema
echo   遥测上行   HTTP /api/hw/telemetry · UDP 8790 · TCP 8791
echo   在环模式   off / shadow / hil（页面顶部状态条可切换）
echo.
echo   没有硬件时，另开一个终端跑设备模拟器：
echo       python -m backend.tools.hw_device_sim --duration 30 --verbose
echo.
echo   Ctrl+C 停止
echo ============================================================
echo.

"%PY%" -m backend.run --port 8787 --scene normal --speed 1.5
pause
