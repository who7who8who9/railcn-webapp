@echo off
setlocal
cd /d "%~dp0"
title railcn 本地服务

rem ---- 1. 把系统目录补回 PATH 头部 ----
rem 这台机器的 PATH 被改坏过：缺 C:/Windows/System32，还留了 C:/Use 这种断行残片，
rem 结果 where / netsh / tasklist 这些系统命令全都找不到。这里只影响本窗口，不改系统设置。
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PATH%"

rem ---- 2. 找 Python ----
rem 不看 where 的结果（PATH 里可能是断的、或指向应用商店空壳），直接试能不能跑；
rem 能跑且版本 >= 3.9 才算。
set "PY="
call :trypy python
call :trypy py
call :trypy "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
call :trypy "C:/Python313/python.exe"
call :trypy "C:/Python312/python.exe"
call :trypy "%ProgramFiles%\Python313\python.exe"
call :trypy "%ProgramFiles%\Python312\python.exe"
if not defined PY goto :nopy

echo 使用 Python: %PY%
echo.

rem ---- 3. 依赖 ----
%PY% -c "import requests" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖 requests ...
  %PY% -m pip install -r "%~dp0..\requirements.txt"
  %PY% -c "import requests" >nul 2>nul
  if errorlevel 1 (
    echo.
    echo [x] 依赖没装上，服务起不来
    echo     手动执行一次：%PY% -m pip install requests
    echo     如果提示没有 pip，先跑：%PY% -m ensurepip
    pause
    exit /b 1
  )
)

rem ---- 4. 启动 ----
echo 启动 railcn 本地服务：http://127.0.0.1:8686/
echo 关掉本窗口或按 Ctrl+C 即停止
echo.
%PY% server.py --port 8686 --open
set "RC=%errorlevel%"
echo.
echo [服务已退出，退出码 %RC%]
pause
exit /b %RC%

:nopy
echo [x] 没找到可用的 Python（要求 3.9 以上）
echo.
echo 已经试过这些位置：
echo   PATH 里的 python / py
echo   %LOCALAPPDATA%\Programs\Python\Python313\python.exe 等 4 个版本
echo   C:/Python313/python.exe
echo   %ProgramFiles%\Python313\python.exe
echo.
echo 当前 PATH 可能有问题，方便时把下面这段发给维护者：
echo ------------------------------------------------------------
echo %PATH%
echo ------------------------------------------------------------
echo 没装 Python 的话：https://www.python.org/downloads/windows/
echo 安装时记得勾选 "Add Python to PATH"
pause
exit /b 1

rem ---- 试跑一个候选：能执行且版本达标才算可用 ----
:trypy
if defined PY goto :eof
%* -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set PY=%*
goto :eof
