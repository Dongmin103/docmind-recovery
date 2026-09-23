param(
    [string]$Distribution = 'Ubuntu',
    [string]$TaskName = 'DocMind Windows Dev WSL KeepAlive'
)

$ErrorActionPreference = 'Stop'

if ($Distribution -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'Distribution must be a simple WSL distribution name.'
}

$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
if (-not (Test-Path -LiteralPath $wsl)) {
    throw 'wsl.exe was not found.'
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$arguments = "-d $Distribution --exec /bin/sleep infinity"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $principalSid = ([Security.Principal.NTAccount]::new($existing.Principal.UserId)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $sameAction = @($existing.Actions).Count -eq 1 -and
        $existing.Actions[0].Execute -ieq $wsl -and
        $existing.Actions[0].Arguments -eq $arguments
    $sameSettings = $existing.Settings.ExecutionTimeLimit -eq 'PT0S' -and
        @($existing.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }).Count -eq 1
    if (-not $sameAction -or -not $sameSettings -or $principalSid -ne $userSid) {
        throw "Task '$TaskName' already exists with different action, trigger, settings, or owner. Inspect it before changing it."
    }
    if ($existing.State -ne 'Running') { Start-ScheduledTask -TaskName $TaskName }
    Write-Output "WSL keep-alive task is $((Get-ScheduledTask -TaskName $TaskName).State) for $Distribution as $user."
    return
}

$action = New-ScheduledTaskAction -Execute $wsl -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Output "WSL keep-alive task started for $Distribution as $user."
