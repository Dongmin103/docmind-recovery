[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$ExpectedUserSid,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$Distribution = 'Ubuntu'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($ExpectedUserSid -notmatch '^S-1-[0-9-]+$' -or $Distribution -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'Probe identity or distribution is invalid.'
}

function Test-ExitZero {
    param([string]$Executable, [string]$Arguments)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.Arguments = $Arguments
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        [void]$process.Start()
        if (-not $process.WaitForExit(20000)) {
            $process.Kill()
            return $false
        }
        return $process.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        $process.Dispose()
    }
}

$result = [ordered]@{
    identity_matches = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq $ExpectedUserSid
    config_readable = $false
    secret_file_exists = $false
    work_root_accessible = $false
    all_source_roots_listable = $false
    wsl_distribution_launches = $false
    wsl_docker_responds = $false
}

try {
    $configFile = [IO.Path]::GetFullPath($ConfigPath)
    $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $result.config_readable = $true
    $result.secret_file_exists = Test-Path -LiteralPath ([string]$config.shared_secret_file) -PathType Leaf
    $result.work_root_accessible = Test-Path -LiteralPath ([string]$config.work_root) -PathType Container
    $rootsListable = @($config.sources).Count -gt 0
    foreach ($source in @($config.sources)) {
        try {
            [void]@(Get-ChildItem -LiteralPath ([string]$source.root) -Force -ErrorAction Stop | Select-Object -First 1).Count
        } catch {
            $rootsListable = $false
        }
    }
    $result.all_source_roots_listable = $rootsListable

    $wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
    $result.wsl_distribution_launches = Test-ExitZero -Executable $wsl -Arguments ("-d {0} --exec /bin/true" -f $Distribution)
    if ($result.wsl_distribution_launches) {
        $result.wsl_docker_responds = Test-ExitZero -Executable $wsl -Arguments ("-d {0} --exec /bin/sh -c `"docker info >/dev/null 2>&1`"" -f $Distribution)
    }
} catch {
    # A failed check remains false. Never serialize the exception or a path.
} finally {
    $result | ConvertTo-Json -Compress | Set-Content -LiteralPath ([IO.Path]::GetFullPath($OutputPath)) -Encoding ASCII
}
