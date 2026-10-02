[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw 'Host worker config file is missing.' }
$acl = Get-Acl -LiteralPath $configFile
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18')
$rules = @($acl.Access)
if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 2) { throw 'HOST_CONFIG_ACL_UNSAFE' }
foreach ($rule in $rules) {
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -notin $allowedSids -or $rule.IsInherited -or $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl) {
        throw 'HOST_CONFIG_ACL_UNSAFE'
    }
}
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.PSObject.Properties.Name -notcontains 'sources' -or @($config.sources).Count -eq 0) { throw 'No configured sources are available.' }
if ($config.PSObject.Properties.Name -notcontains 'sync_state_root') { throw 'sync_state_root is required for the incremental watcher.' }
$sourceIds = @{}
foreach ($source in @($config.sources)) {
    if ([string]::IsNullOrWhiteSpace([string]$source.source_id) -or [string]::IsNullOrWhiteSpace([string]$source.root)) { throw 'Every configured source needs an ID and root.' }
    if ($sourceIds.ContainsKey([string]$source.source_id)) { throw 'Duplicate source_id in host config.' }
    $sourceIds[[string]$source.source_id] = $true
}

$shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $shell -PathType Leaf)) { throw 'Task PowerShell executable is unavailable.' }
if ($configFile.Contains('"') -or $PSScriptRoot.Contains('"')) { throw 'Task paths containing quotation marks are unsupported.' }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited

function New-DocMindTaskSettings {
    param([switch]$Disabled)
    $options = @{
        StartWhenAvailable = $true
        MultipleInstances = 'IgnoreNew'
        RestartInterval = (New-TimeSpan -Minutes 15)
        RestartCount = 8
        ExecutionTimeLimit = [TimeSpan]::Zero
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
    }
    if ($Disabled) { $options.Disable = $true }
    return New-ScheduledTaskSettingsSet @options
}

function New-DocMindRepeatedTrigger {
    param([int]$Minutes)
    # StartBoundary is the Windows local midnight. The long repetition window
    # restarts a stopped worker after reboot/logon and retries missed claims.
    return New-ScheduledTaskTrigger -Once -At ([DateTime]::Today) -RepetitionInterval (New-TimeSpan -Minutes $Minutes) -RepetitionDuration (New-TimeSpan -Days 3650)
}

function Register-DocMindTask {
    param([string]$Name, [string]$Script, [string]$Arguments, [int]$RepeatMinutes, [switch]$AtLogon, [switch]$Disabled)
    $scriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $Script))
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw 'Task script is unavailable.' }
    $taskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "{0}" -ConfigPath "{1}" {2}' -f $scriptPath, $configFile, $Arguments
    $action = New-ScheduledTaskAction -Execute $shell -Argument $taskArguments -WorkingDirectory $PSScriptRoot
    $triggers = @(New-DocMindRepeatedTrigger -Minutes $RepeatMinutes)
    if ($AtLogon) { $triggers += New-ScheduledTaskTrigger -AtLogOn -User $identity }
    $settings = New-DocMindTaskSettings -Disabled:$Disabled
    [void](Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force)
    [pscustomobject]@{ task_name = $Name; principal = $identity; logon_type = 'Interactive'; repeat_minutes = $RepeatMinutes; at_logon = [bool]$AtLogon; enabled = -not [bool]$Disabled; source_count = $sourceIds.Count }
}

$worker = Register-DocMindTask -Name 'DocMind Host Worker' -Script 'Start-DocMindUEncryptorHostWorker.ps1' -Arguments '' -RepeatMinutes 15 -AtLogon
$watcher = Register-DocMindTask -Name 'DocMind Source Watcher' -Script 'Watch-DocMindEncryptedSources.ps1' -Arguments '' -RepeatMinutes 15 -AtLogon
$worker
$watcher
