[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [switch]$EnableDiscovery
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw 'Host worker config file is missing.' }
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.PSObject.Properties.Name -notcontains 'sources' -or @($config.sources).Count -eq 0) { throw 'No configured sources are available.' }
$sourceIds = @{}
foreach ($source in @($config.sources)) {
    if ([string]::IsNullOrWhiteSpace([string]$source.source_id) -or [string]::IsNullOrWhiteSpace([string]$source.root)) { throw 'Every configured source needs an ID and root.' }
    if ($sourceIds.ContainsKey([string]$source.source_id)) { throw 'Duplicate source_id in host config.' }
    $sourceIds[[string]$source.source_id] = $true
}

$plan = & (Join-Path $PSScriptRoot 'Get-DocMindReconciliationTaskPlan.ps1') -ConfigPath $configFile
$shell = [string]$plan.execute
if (-not (Test-Path -LiteralPath $shell -PathType Leaf)) { throw 'Task PowerShell executable is unavailable.' }
if ($configFile.Contains('"') -or $PSScriptRoot.Contains('"')) { throw 'Task paths containing quotation marks are unsupported.' }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartInterval (New-TimeSpan -Minutes 15) -RestartCount 8 -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

function New-DocMindRepeatedTrigger {
    param([int]$Minutes)
    # StartBoundary is the Windows local midnight. The long repetition window
    # restarts a stopped worker after reboot/logon and retries missed claims.
    return New-ScheduledTaskTrigger -Once -At ([DateTime]::Today) -RepetitionInterval (New-TimeSpan -Minutes $Minutes) -RepetitionDuration (New-TimeSpan -Days 3650)
}

function Register-DocMindTask {
    param([string]$Name, [string]$Script, [string]$Arguments, [int]$RepeatMinutes)
    $scriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $Script))
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw 'Task script is unavailable.' }
    $taskArguments = '-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "{0}" -ConfigPath "{1}" {2}' -f $scriptPath, $configFile, $Arguments
    $action = New-ScheduledTaskAction -Execute $shell -Argument $taskArguments -WorkingDirectory $PSScriptRoot
    $trigger = New-DocMindRepeatedTrigger -Minutes $RepeatMinutes
    [void](Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force)
    [pscustomobject]@{ task_name = $Name; principal = $identity; logon_type = 'Interactive'; repeat_minutes = $RepeatMinutes; source_count = $sourceIds.Count }
}

$worker = Register-DocMindTask -Name 'DocMind Host Worker' -Script 'Start-DocMindUEncryptorHostWorker.ps1' -Arguments '' -RepeatMinutes 15
$watchArguments = if ($EnableDiscovery) { '-EnableDiscovery' } else { '' }
$watcher = Register-DocMindTask -Name 'DocMind Source Watcher' -Script 'Watch-DocMindEncryptedSources.ps1' -Arguments $watchArguments -RepeatMinutes 15
$reconciliation = Register-DocMindTask -Name ([string]$plan.task_name) -Script 'Invoke-DocMindSourceReconciliation.ps1' -Arguments '-Reason scheduled' -RepeatMinutes ([int]$plan.repeat_every_minutes)
$worker
$watcher
$reconciliation
