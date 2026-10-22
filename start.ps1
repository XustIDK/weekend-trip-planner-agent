<#
.SYNOPSIS
    周末去哪儿 · 行程规划助手 —— Windows PowerShell 启动脚本

.DESCRIPTION
    检查 Python、准备 .env、设置 UTF-8 控制台并启动本地服务（默认 http://127.0.0.1:8765）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start.ps1

.EXAMPLE
    .\start.ps1 -Port 8766 -NoBrowser
#>
[CmdletBinding()]
param(
    # 覆盖端口；留空(0)则使用 .env 中的 PORT（默认 8765）
    [int]$Port = 0,
    # 不自动打开浏览器
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 1) 控制台统一 UTF-8，避免中文乱码
try { $null = & chcp.com 65001 2>&1 } catch { }
try { [Console]::InputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# 2) 定位 Python 3.10+
function Find-Python {
    foreach ($name in @("python", "py")) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $cmd) { continue }

        $prefix = @()
        if ($cmd.Name -match '^py(\.exe)?$') { $prefix = @("-3") }

        try {
            $versionText = (& $cmd.Source @prefix "--version" 2>&1 | Out-String).Trim()
        }
        catch {
            continue
        }

        if ($versionText -match "Python\s+3\.(\d+)") {
            if ([int]$Matches[1] -ge 10) {
                return [pscustomobject]@{ Exe = $cmd.Source; Prefix = $prefix; Version = $versionText }
            }
            Write-Host "[提示] 检测到 $versionText，本程序需要 Python 3.10 或更高版本。" -ForegroundColor Yellow
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "[错误] 未找到可用的 Python 3.10+。" -ForegroundColor Red
    Write-Host "       请从 https://www.python.org/downloads/windows/ 安装，" -ForegroundColor Yellow
    Write-Host "       安装时勾选 'Add python.exe to PATH'，然后重新运行本脚本。" -ForegroundColor Yellow
    exit 1
}
Write-Host "[信息] 使用 $($python.Version)" -ForegroundColor DarkGray

# 3) 准备 .env
if (-not (Test-Path -LiteralPath ".env" -PathType Leaf)) {
    if (Test-Path -LiteralPath ".env.example" -PathType Leaf) {
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
        Write-Host "[提示] 已根据 .env.example 生成 .env。" -ForegroundColor Yellow
    }
    else {
        Write-Host "[错误] 缺少 .env 与 .env.example，无法读取配置。" -ForegroundColor Red
        exit 1
    }
}

$envText = Get-Content -LiteralPath ".env" -Raw -Encoding UTF8
$keyLine = ($envText -split "`r?`n" |
        Where-Object { $_ -match '^\s*OPENAI_API_KEY\s*=' } |
        Select-Object -First 1)
if (-not $keyLine -or $keyLine -match 'your_api_key_here' -or $keyLine -match '=\s*$') {
    Write-Host "[提示] 尚未填写 OPENAI_API_KEY：页面可以打开，但对话会提示未配置模型。" -ForegroundColor Yellow
    Write-Host "       编辑方式： notepad .env" -ForegroundColor Yellow
}

# 4) 运行参数（进程环境变量优先于 .env）
if ($Port -gt 0) { $env:PORT = "$Port" }
if ($NoBrowser) { $env:OPEN_BROWSER = "0" } else { $env:OPEN_BROWSER = "1" }

# 5) 启动服务（前台运行，Ctrl+C 停止）
try {
    & $python.Exe @($python.Prefix) "app.py"
    exit $LASTEXITCODE
}
catch {
    Write-Host "[错误] 启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
