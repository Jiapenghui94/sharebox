@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 打包 ShareBox 原生安装器

echo ============================================================
echo   打包 ShareBox 安装包（原生安装器，非单文件自解压）
echo   需要：Python 3.9+、联网（第一次装 PyInstaller）、Windows 自带 csc
echo   产物：ShareBox-Setup.exe
echo ============================================================
echo.
pause

python --version >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python，请先安装并勾选 Add to PATH。
    pause
    exit /b 1
)

echo [1/4] 安装 PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto failed

echo [2/4] 构建目录版程序（不依赖运行时解压到临时目录）...
python -m PyInstaller --noconfirm --clean --onedir --console ^
    --name ShareBox ^
    --icon "assets\sharebox.ico" ^
    --add-data "sharebox\web;sharebox\web" ^
    --add-data "assets\sharebox.ico;assets" ^
    --hidden-import sharebox.cli ^
    --hidden-import sharebox.hub ^
    --hidden-import sharebox.peer ^
    --hidden-import sharebox.syncer ^
    launcher.py
if errorlevel 1 goto failed

echo [3/4] 打包程序文件...
if not exist "installer" mkdir "installer"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\ShareBox\*' -DestinationPath 'installer\payload.zip' -Force"
if errorlevel 1 goto failed

echo [4/4] 编译原生安装器...
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
"%CSC%" /nologo /target:winexe /out:ShareBox-Setup.exe ^
    /r:System.Windows.Forms.dll /r:System.Drawing.dll ^
    /r:System.IO.Compression.dll /r:Microsoft.CSharp.dll ^
    /resource:installer\payload.zip,payload.zip installer\Installer.cs
if errorlevel 1 goto failed

echo.
echo 打包完成：ShareBox-Setup.exe
echo 双击它就是安装向导；安装日志在 %%LOCALAPPDATA%%\ShareBox\install.log
echo.
pause
exit /b 0

:failed
echo.
echo 打包失败，请检查上面的错误信息。
pause
exit /b 1
