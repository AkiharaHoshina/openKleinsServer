<#
    一键重打包 bizserver.exe / fakeserver.exe

        powershell -File tools\build_release.ps1

    产物: <仓库根>\release\{bizserver,fakeserver}.exe
    中间: <仓库根>\_build\{bizserver,fakeserver}\

    ★★ 为什么必须先停服务端
       exe 正在跑时文件被占用，PyInstaller 覆盖会报
       `PermissionError: [WinError 5] 拒绝访问: ...\bizserver.exe`。
       注意：**不是游戏在跑，是服务端自己占着文件**。

    需要: Python 3.8.6 + PyInstaller 3.6
#>
[CmdletBinding()]
param(
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host ""
Write-Host "  openKleinsServer 重打包" -ForegroundColor Cyan
Write-Host "  仓库根: $Root"
Write-Host ""

# ── 前置检查 ────────────────────────────────────────────
if (-not $SkipChecks) {
    # 1) 游戏在不在跑？（在跑也不要紧，但提醒一下）
    $game = Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -eq "环行旅舍" }
    if ($game) {
        Write-Host "  [提示] 游戏正在运行（PID $($game.Id -join ',')）。" -ForegroundColor Yellow
        Write-Host "         重打包本身不受影响，但等你重启服务端时会断掉它的连接。" -ForegroundColor Yellow
        Write-Host ""
    }

    # 2) ★ 服务端在不在跑？—— 在跑就必须先停，否则 WinError 5
    $srv = Get-Process -Name bizserver, fakeserver -ErrorAction SilentlyContinue
    if ($srv) {
        Write-Host "  [中止] 服务端还在运行，文件被占用，无法覆盖 exe：" -ForegroundColor Red
        $srv | ForEach-Object { Write-Host ("         " + $_.ProcessName + " (PID " + $_.Id + ")") }
        Write-Host ""
        Write-Host "  请先运行  停止服务端.bat  ，或加 -SkipChecks 跳过（不推荐）。" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  [OK] 服务端没在跑" -ForegroundColor Green
}

# 3) PyInstaller 在不在
$pyi = $null
try { $pyi = (python -m PyInstaller --version) 2>&1 } catch {}
if (-not $pyi) {
    Write-Host "  [中止] 找不到 PyInstaller。安装:  pip install pyinstaller==3.6" -ForegroundColor Red
    exit 1
}
Write-Host "  [OK] PyInstaller $pyi"
Write-Host ""

# ── 构建 ────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path "$Root\release" | Out-Null

foreach ($spec in @("bizserver", "fakeserver")) {
    Write-Host "  ── 构建 $spec ──" -ForegroundColor Cyan
    python -m PyInstaller `
        --distpath "$Root\release" `
        --workpath "$Root\_build" `
        --noconfirm --log-level WARN `
        "$Root\tools\$spec.spec"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [失败] $spec 构建返回 $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host ""
Write-Host "  ── 产物 ──" -ForegroundColor Cyan
Get-ChildItem "$Root\release" -Filter *.exe |
    ForEach-Object { Write-Host ("    " + $_.Name + "  " + [math]::Round($_.Length/1MB, 2) + " MB  " + $_.LastWriteTime) }

# ── 验证 ────────────────────────────────────────────────
Write-Host ""
Write-Host "  ── 验证包里真的有新代码 ──" -ForegroundColor Cyan
python "$Root\tools\_verify_exe.py" bizserver bizserver
python "$Root\tools\_verify_exe.py" fakeserver fakeserver

Write-Host ""
Write-Host "  完成。把 release\ 里的 exe 拷到仓库根（或直接用源码跑）即可。" -ForegroundColor Green
Write-Host ""
