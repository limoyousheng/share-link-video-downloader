$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $projectRoot 'requirements.txt'
$requirementsStamp = Join-Path $venvDir '.requirements.sha256'

Set-Location -LiteralPath $projectRoot
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Get-FileSha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($bytes)).Replace('-', '')
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host '首次运行：正在创建独立 Python 环境…' -ForegroundColor Cyan
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
        & $uvCommand.Source venv --python 3.12 $venvDir
        if ($LASTEXITCODE -ne 0) { throw 'uv 创建 Python 环境失败。' }
    }
    else {
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            & $pyLauncher.Source -3.12 -m venv $venvDir
        }
        else {
            $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
            if (-not $pythonCommand) { throw '未找到 Python。请安装 Python 3.12 或 uv 后重试。' }
            & $pythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
            if ($LASTEXITCODE -ne 0) { throw '当前 python 不是 3.12。请安装 Python 3.12 或 uv 后重试。' }
            & $pythonCommand.Source -m venv $venvDir
        }
        if ($LASTEXITCODE -ne 0) { throw 'Python 创建虚拟环境失败。' }
    }
}

$currentHash = Get-FileSha256 $requirements
$installedHash = if (Test-Path -LiteralPath $requirementsStamp) {
    (Get-Content -LiteralPath $requirementsStamp -Raw).Trim()
} else { '' }

if ($currentHash -ne $installedHash) {
    Write-Host '正在安装或更新程序组件，首次运行可能需要几分钟…' -ForegroundColor Cyan
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
        & $uvCommand.Source pip install --python $venvPython -r $requirements
    }
    else {
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install -r $requirements
    }
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络后重试。' }
    Set-Content -LiteralPath $requirementsStamp -Value $currentHash -Encoding ASCII
}

Write-Host ''
Write-Host '视频下载服务正在启动。关闭本窗口即可停止服务。' -ForegroundColor Green
& $venvPython -m scripts.serve
$serverExitCode = $LASTEXITCODE
if ($serverExitCode -ne 0) {
    throw "服务进程异常退出，错误码：$serverExitCode"
}
