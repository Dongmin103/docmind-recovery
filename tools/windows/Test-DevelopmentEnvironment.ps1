[CmdletBinding()]
param(
    [string]$DecryptorPath,
    [string]$ReportPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $ReportPath) {
    $ReportPath = Join-Path $repoRoot '.local/windows-preflight.json'
}

$gitCommand = Get-Command git -ErrorAction SilentlyContinue
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerVersion = $null
$composeVersion = $null
$engineOS = $null
$issues = @()

if ($gitCommand) {
    $gitVersion = (& $gitCommand.Source --version 2>$null | Out-String).Trim()
} else {
    $gitVersion = $null
    $issues += 'Git is not installed or not on PATH.'
}
if ($dockerCommand) {
    try {
    $dockerVersion = (& $dockerCommand.Source --version 2>$null | Out-String).Trim()
    $composeVersion = (& $dockerCommand.Source compose version 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { $issues += 'Docker Compose is unavailable.' }
    $engineOS = (& $dockerCommand.Source info --format '{{.OSType}}' 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { $issues += 'Docker engine is not reachable.' }
    elseif ($engineOS -ne 'linux') { $issues += 'This source needs a Linux container engine.' }
    } catch { $issues += 'Docker/Compose could not be queried. Check the local installation and engine status.' }
} else {
    $issues += 'Docker is not installed or not on PATH.'
}

$roots = @(
    'D:\UPLEXSOFT\UDRIVE\USER\TEST1',
    'D:\UPLEXSOFT\UDRIVE\DEPT\_DEPT_2',
    'D:\UPLEXSOFT\UDRIVE\DEPT\_DEPT_1'
)
$sourceStatus = @($roots | ForEach-Object {
    [ordered]@{ path = $_; exists = (Test-Path -LiteralPath $_ -PathType Container) }
})
$decryptor = $null
if ($DecryptorPath) {
    if (Test-Path -LiteralPath $DecryptorPath -PathType Leaf) {
        $file = Get-Item -LiteralPath $DecryptorPath
        $decryptor = [ordered]@{
            path = $file.FullName
            version = $file.VersionInfo.FileVersion
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            executed = $false
        }
    } else { $issues += 'The explicitly supplied decryptor path does not exist.' }
}
$report = [ordered]@{
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    os = [Environment]::OSVersion.VersionString
    logical_processors = [Environment]::ProcessorCount
    git = $gitVersion
    docker = $dockerVersion
    compose = $composeVersion
    engine_os = $engineOS
    sources = $sourceStatus
    decryptor = $decryptor
    issues = $issues
    note = 'Read-only preflight. No source enumeration, decryption, service changes, or container startup.'
}
$parent = Split-Path -Parent ([IO.Path]::GetFullPath($ReportPath))
[IO.Directory]::CreateDirectory($parent) | Out-Null
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
Write-Output "Preflight report: $ReportPath"
if ($issues.Count) {
    $issues | ForEach-Object { Write-Warning $_ }
    exit 1
}
Write-Output 'Git and Linux Docker prerequisites are reachable. Application and decryptor execution remain untested.'
