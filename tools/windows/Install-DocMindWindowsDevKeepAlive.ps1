param(
    [string]$Distribution = 'Ubuntu',
    [string]$TaskName = 'DocMind Windows Dev WSL KeepAlive'
)

$ErrorActionPreference = 'Stop'

if ($Distribution -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'Distribution must be a simple WSL distribution name.'
}

$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$watchdog = Join-Path $PSScriptRoot 'Keep-DocMindWindowsDevWslRunning.ps1'
if (-not (Test-Path -LiteralPath $powershell) -or -not (Test-Path -LiteralPath $watchdog)) {
    throw 'PowerShell or the WSL keep-alive watchdog was not found.'
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdog`" -Distribution $Distribution"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $principalSid = ([Security.Principal.NTAccount]::new($existing.Principal.UserId)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $sameAction = @($existing.Actions).Count -eq 1 -and
        $existing.Actions[0].Execute -ieq $powershell -and
        $existing.Actions[0].Arguments -eq $arguments
    $sameSettings = $existing.Settings.ExecutionTimeLimit -eq 'PT0S' -and
        $existing.Settings.RestartCount -eq 999 -and
        $existing.Settings.RestartInterval -eq 'PT1M' -and
        @($existing.Triggers).Count -eq 1 -and
        @($existing.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }).Count -eq 1
    if ($principalSid -ne $userSid) {
        throw "Task '$TaskName' belongs to another user. Inspect it before changing it."
    }
    if (-not $sameAction -and -not (@($existing.Actions).Count -eq 1 -and
            $existing.Actions[0].Execute -ieq (Join-Path $env:SystemRoot 'System32\wsl.exe') -and
            $existing.Actions[0].Arguments -eq "-d $Distribution --exec /bin/sleep infinity")) {
        throw "Task '$TaskName' already exists with different action, trigger, settings, or owner. Inspect it before changing it."
    }
    if ($sameAction -and $sameSettings) {
        if ($existing.State -ne 'Running') { Start-ScheduledTask -TaskName $TaskName }
        Write-Output "WSL keep-alive task is $((Get-ScheduledTask -TaskName $TaskName).State) for $Distribution as $user."
        return
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Output "WSL keep-alive task started for $Distribution as $user."
