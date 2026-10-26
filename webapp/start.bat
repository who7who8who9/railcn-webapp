@echo off
setlocal
cd /d "%~dp0"
title railcn 本地服务

rem 依次尝试 python / py / python3，避免写死本机路径
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY where python3 >nul 2>nul && set "PY=python3"

if not defined PY (
  echo [x] 未找到 Python，请安装 Python 3.9+ 并勾选 "Add Python to PATH"
  echo     下载地址：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

rem 应用商店那个 python.exe 是空壳，执行会弹商店；这里先验一次真能跑
%PY% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [x] 找到的 %PY% 执行不了（可能是应用商店的空壳），或版本低于 3.9
  echo     请装 Python 3.9+ 并勾选 "Add Python to PATH"
  pause
  exit /b 1
)

%PY% -c "import requests" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖 ...
  %PY% -m pip install -r "%~dp0..\requirements.txt"
  %PY% -c "import requests" >nul 2>nul
  if errorlevel 1 (
    echo [x] 依赖没装上（多半是没网或被代理挡住），服务起不来
    echo     手动装一次：%PY% -m pip install requests
    pause
    exit /b 1
  )
)

echo 启动 railcn 本地服务：http://127.0.0.1:8686/
echo 关掉本窗口或按 Ctrl+C 即停止
%PY% server.py --port 8686 --open
set "RC=%errorlevel%"
echo.
echo [服务已退出，退出码 %RC%]
pause
