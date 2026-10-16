@echo off
chcp 936 >nul
title 环行旅舍 假服务端
cd /d "%~dp0"

echo.
echo   ==================================================
echo     《环行旅舍》本地假服务端
echo   ==================================================
echo.

REM 优先用同目录下的 exe；没有 exe 就退回 python 直接跑（需要 PATH 里有 python）。
set RUN_H=
set RUN_B=
if exist "%~dp0fakeserver.exe" (set RUN_H="%~dp0fakeserver.exe") else (set RUN_H=python fakeserver.py)
if exist "%~dp0bizserver.exe"  (set RUN_B="%~dp0bizserver.exe")  else (set RUN_B=python bizserver.py)

REM 只认 LISTENING 行来判断端口是否已被占用，已监听的那个不重复启动。
set BUSY_H=
set BUSY_B=
netstat -ano | findstr LISTENING | findstr /C:":10300 " >nul 2>&1 && set BUSY_H=1
netstat -ano | findstr LISTENING | findstr /C:":10301" >nul 2>&1 && set BUSY_B=1

if defined BUSY_H (echo   [跳过] HTTP 层 10300 已经在运行) else (echo   [启动] HTTP 层 10300 ... & start "Kleins-HTTP-10300" /min %RUN_H%)
if defined BUSY_B (echo   [跳过] 业务层 10301 已经在运行) else (echo   [启动] 业务层 10301 ... & start "Kleins-BIZ-10301" /min %RUN_B%)

echo.
echo   等待两个端口就绪（exe 首次启动会慢一点，通常 3-8 秒）...

set /a W=0
:wait
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr LISTENING | findstr /C:":10300 " >nul 2>&1 || goto retry
netstat -ano | findstr LISTENING | findstr /C:":10301" >nul 2>&1 || goto retry
goto ready

:retry
set /a W+=1
if %W% lss 20 goto wait
echo   [X] 等待 20 次仍未就绪 —— 请看本目录下的 probe.log / bizserver.log
goto done

:ready
echo   [OK] HTTP 层  127.0.0.1:10300
echo   [OK] 业务层  127.0.0.1:10301

:done
echo.
echo   日志    : probe.log   bizserver.log
echo   关闭    : 双击 停止服务端.bat
echo   下一步  : 启动游戏（用打过补丁的客户端）
echo.
ping -n 9 127.0.0.1 >nul