param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> 安装 PyInstaller"
if (-not $SkipInstall) {
    python -m pip install --upgrade pyinstaller
}

Write-Host "==> 构建目录版程序"
python -m PyInstaller --noconfirm --clean --onedir --console `
    --name ShareBox `
    --icon "assets\sharebox.ico" `
    --add-data "sharebox\web;sharebox\web" `
    --add-data "assets\sharebox.ico;assets" `
    --hidden-import sharebox.cli `
    --hidden-import sharebox.hub `
    --hidden-import sharebox.peer `
    --hidden-import sharebox.syncer `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "==> 打包程序文件"
if (Test-Path "installer\payload.zip") { Remove-Item "installer\payload.zip" -Force }
Compress-Archive -Path "dist\ShareBox\*" -DestinationPath "installer\payload.zip" -Force

Write-Host "==> 编译安装器"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
& $csc /nologo /target:winexe /out:ShareBox-Setup.exe `
    /r:System.Windows.Forms.dll /r:System.Drawing.dll `
    /r:System.IO.Compression.dll /r:Microsoft.CSharp.dll `
    /resource:installer\payload.zip,payload.zip installer\Installer.cs
if ($LASTEXITCODE -ne 0) { throw "csc failed" }

Write-Host "==> 完成"
Get-Item ShareBox-Setup.exe | Format-List Name, Length, LastWriteTime
