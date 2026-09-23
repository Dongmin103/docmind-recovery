param(
    [string]$Distribution = 'Ubuntu',
    [int]$RetryDelaySeconds = 5
)

$ErrorActionPreference = 'Stop'

if ($Distribution -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'Distribution must be a simple WSL distribution name.'
}
if ($RetryDelaySeconds -lt 1) {
    throw 'RetryDelaySeconds must be positive.'
}

$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
if (-not (Test-Path -LiteralPath $wsl)) {
    throw 'wsl.exe was not found.'
}

while ($true) {
    & $wsl -d $Distribution --exec /bin/sleep infinity
    Start-Sleep -Seconds $RetryDelaySeconds
}
