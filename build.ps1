$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$spec = Join-Path $root 'search-mcp.spec'
if (-not (Test-Path -LiteralPath $spec -PathType Leaf)) {
    throw "构建配置不存在: $spec"
}
$python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $python) {
    throw '未找到 python.exe，请先安装 Python 3.10+。'
}

Push-Location -LiteralPath $root
try {
    & $python.Source -m PyInstaller --noconfirm search-mcp.spec
    # PowerShell 的 Stop 不代替外部程序退出码检查，需要显式判断构建失败。
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出码: $LASTEXITCODE"
    }

    $output = Join-Path $root 'dist\search-mcp'
    $exe = Join-Path $output 'search-mcp.exe'
    $internal = Join-Path $output '_internal'
    # 目录发行版必须同时包含启动程序和依赖目录，命令结束不等于产物完整。
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $internal -PathType Container)) {
        throw "构建命令已结束，但产物不完整: $output"
    }
    Write-Host "构建成功: $output"
}
finally {
    Pop-Location
}
