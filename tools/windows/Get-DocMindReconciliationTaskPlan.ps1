[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$TaskName = 'DocMind source reconciliation (Asia-Seoul midnight)'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw 'Host worker config file is missing.' }
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
$timeZone = if ($config.PSObject.Properties.Name -contains 'daily_reconciliation_timezone') { [string]$config.daily_reconciliation_timezone } else { 'Asia/Seoul' }
$localTime = if ($config.PSObject.Properties.Name -contains 'daily_reconciliation_local_time') { [string]$config.daily_reconciliation_local_time } else { '00:00' }
if ($timeZone -ne 'Asia/Seoul' -or $localTime -ne '00:00') { throw 'Daily reconciliation must remain scheduled for 00:00 Asia/Seoul.' }
if ([TimeZoneInfo]::Local.Id -ne 'Korea Standard Time') { throw 'Windows must use Korea Standard Time before creating the local-midnight trigger.' }

$scanner = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'Invoke-DocMindSourceReconciliation.ps1'))
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) { throw 'Windows PowerShell executable is unavailable.' }
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "{0}" -ConfigPath "{1}" -Reason scheduled' -f $scanner, $configFile
[pscustomobject][ordered]@{
    task_name = $TaskName
    execute = $powershell
    arguments = $arguments
    trigger = 'Daily at 00:00 local time'
    repeat_every_minutes = 15
    repeat_duration_hours = 24
    required_windows_time_zone = 'Korea Standard Time'
    start_when_available = $true
    multiple_instances = 'IgnoreNew'
    restart_on_failure_interval_minutes = 15
    restart_on_failure_count = 8
    registration_performed = $false
}
