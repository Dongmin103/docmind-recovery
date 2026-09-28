[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [ValidateSet('home-test1-e2e', 'dept-1-e2e')]
    [string[]]$SourceIds = @('home-test1-e2e', 'dept-1-e2e'),
    [switch]$PlanOnly
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
$configuredIds = @{}
foreach ($source in @($config.sources)) {
    $sourceId = [string]$source.source_id
    if ([string]::IsNullOrWhiteSpace($sourceId) -or $configuredIds.ContainsKey($sourceId)) { throw 'Missing or duplicate source_id in host config.' }
    $configuredIds[$sourceId] = $true
}
$selectedIds = @($SourceIds | Select-Object -Unique)
if ($selectedIds.Count -ne $SourceIds.Count) { throw 'Duplicate selected source_id.' }
foreach ($sourceId in $selectedIds) {
    if (-not $configuredIds.ContainsKey($sourceId)) { throw 'Selected source_id is absent from host config.' }
}

$taskPlan = & (Join-Path $PSScriptRoot 'Get-DocMindReconciliationTaskPlan.ps1') -ConfigPath $configFile
$shell = [string]$taskPlan.execute
if (-not (Test-Path -LiteralPath $shell -PathType Leaf)) { throw 'Task PowerShell executable is unavailable.' }
if ($configFile.Contains('"') -or $PSScriptRoot.Contains('"')) { throw 'Task paths containing quotation marks are unsupported.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited

function New-ScopedTrigger {
    $trigger = New-ScheduledTaskTrigger -Once -At ([DateTime]::Today) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
    return $trigger
}

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartInterval (New-TimeSpan -Minutes 15) -RestartCount 8 -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Disable
$definitions = @(
    foreach ($sourceId in $selectedIds) {
        [pscustomobject]@{
            task_name = "DocMind Source Watcher ($sourceId)"
            source_id = $sourceId
            kind = 'discovery'
            script = 'Watch-DocMindEncryptedSources.ps1'
            extra_arguments = "-SourceId $sourceId -EnableDiscovery"
            at_logon = $true
        }
        [pscustomobject]@{
            task_name = "DocMind source reconciliation (Asia-Seoul midnight, $sourceId)"
            source_id = $sourceId
            kind = 'scheduled-reconciliation'
            script = 'Invoke-DocMindSourceReconciliation.ps1'
            extra_arguments = "-Reason scheduled -SourceId $sourceId"
            at_logon = $false
        }
    }
)

foreach ($definition in $definitions) {
    $scriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $definition.script))
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw 'Task script is unavailable.' }
    $definition | Add-Member -NotePropertyName script_path -NotePropertyValue $scriptPath
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "{0}" -ConfigPath "{1}" {2}' -f $scriptPath, $configFile, $definition.extra_arguments
    $definition | Add-Member -NotePropertyName arguments -NotePropertyValue $arguments
}

if (-not $PlanOnly) {
    foreach ($definition in $definitions) {
        if (Get-ScheduledTask -TaskName $definition.task_name -ErrorAction SilentlyContinue) { throw 'A scoped task already exists; no task was changed.' }
    }
    $created = @()
    try {
        foreach ($definition in $definitions) {
            $action = New-ScheduledTaskAction -Execute $shell -Argument $definition.arguments -WorkingDirectory $PSScriptRoot
            $triggers = @(New-ScopedTrigger)
            if ($definition.at_logon) { $triggers += New-ScheduledTaskTrigger -AtLogOn -User $identity }
            [void](Register-ScheduledTask -TaskName $definition.task_name -Action $action -Trigger $triggers -Settings $settings -Principal $principal -ErrorAction Stop)
            $created += $definition.task_name
        }
    } catch {
        foreach ($taskName in $created) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }
        throw
    }
}

$definitions | ForEach-Object {
    [pscustomobject]@{
        task_name = $_.task_name
        source_id = $_.source_id
        kind = $_.kind
        enabled = $false
        principal = $identity
        logon_type = 'Interactive'
        action_arguments = $_.arguments
        registered = -not [bool]$PlanOnly
    }
}
