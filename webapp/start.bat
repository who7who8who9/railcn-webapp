@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 依次尝试 python / py / python3，避免写死本机路径
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY where python3 >nul 2>nul && set "PY=python3"

if not defined PY (
  echo [x] 未找到 Python，请安装 Python 3.9+ 并勾选 "Add Python to PATH"
  pause
  exit /b 1
)

%PY% -c "import requests" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖 ...
  %PY% -m pip install -r "%~dp0..\requirements.txt"
)

echo 启动 railcn 本地服务：http://127.0.0.1:8686/
%PY% server.py --port 8686 --open
pause
