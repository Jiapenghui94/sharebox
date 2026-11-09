@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ShareBox 共享中心
call :findpython
if errorlevel 1 goto nopython

echo ============================================================
echo   ShareBox 共享中心
echo   首次启动会让你设置管理员密码，之后请记住它。
echo   保持这个窗口开着，其他同事才能访问；按 Ctrl+C 停止。
echo ============================================================
echo.
%PY% sb.py hub %*
echo.
echo 共享中心已停止运行。
pause
exit /b 0

:findpython
python --version >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    exit /b 0
)
py -3 --version >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    exit /b 0
)
exit /b 1

:nopython
echo.
echo [错误] 没有找到 Python。
echo.
echo 请先安装 Python 3.9 或更高版本：
echo     https://www.python.org/downloads/
echo 安装时务必勾选 "Add python.exe to PATH"。
echo.
echo 或者让 IT 人员运行 build-exe.bat，打包成免安装的 ShareBox.exe。
echo.
pause
exit /b 1
