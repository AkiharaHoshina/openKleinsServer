@echo off
chcp 936 >nul
title 环行旅舍 假服务端 - 停止

taskkill /F /IM fakeserver.exe >nul 2>&1
taskkill /F /IM bizserver.exe  >nul 2>&1

REM 兜底：按端口杀（对付用 python xxx.py 直接跑的情况）
for /f "tokens=5" %%p in ('netstat -ano ^| findstr LISTENING ^| findstr /C:":10300 "') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr LISTENING ^| findstr /C:":10301"') do taskkill /F /PID %%p >nul 2>&1

echo   假服务端已停止。
timeout /t 3 >nul