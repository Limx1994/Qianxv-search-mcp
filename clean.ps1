[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$rootPrefix = $root + '\'
$targets = @(
    (Join-Path $root 'build'),
    (Join-Path $root 'dist'),
    (Join-Path $root '.pytest_cache'),
    (Join-Path $root '.ruff_cache')
)

foreach ($dir in @('', 'providers', 'tests', 'release')) {
    $parent = Join-Path $root $dir
    $targets += Join-Path $parent '__pycache__'
    if (Test-Path -LiteralPath $parent -PathType Container) {
        $targets += Get-ChildItem -LiteralPath $parent -File -Filter '*.pyc' -Force |
            Select-Object -ExpandProperty FullName
    }
}

$removed = 0
$missing = 0
$preview = 0
$failed = 0
try {
    foreach ($target in $targets) {
        $path = [IO.Path]::GetFullPath($target)
        # 带尾部分隔符的根路径前缀避免误匹配同名前缀目录，删除前先校验边界。
        if (-not $path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "清理目标超出项目目录: $path"
        }
        if (-not (Test-Path -LiteralPath $path)) {
            $missing++
            continue
        }
        $item = Get-Item -LiteralPath $path -Force
        # 拒绝符号链接或目录联接，避免递归删除通过链接触及其他位置。
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "拒绝清理链接目标: $path"
        }
        if (-not $PSCmdlet.ShouldProcess($path, '删除')) {
            # 支持 -WhatIf 和确认取消，并将这些情况计入跳过统计。
            $preview++
            continue
        }
        Remove-Item -LiteralPath $path -Recurse -Force
        $removed++
        Write-Host "已清理: $path"
    }
}
catch {
    $failed++
    throw
}
finally {
    Write-Host "清理结果: 删除 $removed 项；跳过 $($missing + $preview) 项（不存在 $missing，预览或未确认 $preview）；失败 $failed 项。"
}
