Set-StrictMode -Version Latest

function Invoke-DocMindPool {
    param([string]$Executable, [string[]]$Arguments, [string]$StopFile, [switch]$Once)
    if ($Arguments.Count -lt 1 -or $Arguments.Count -gt 12) { throw 'POOL_SIZE_INVALID' }
    $children = @{}; $nextStart = @{}; $completedSlots = @{}
    $started = 0; $completed = 0; $peak = 0
    try {
        while ($true) {
            $stopping = Test-Path -LiteralPath $StopFile
            for ($slot = 0; $slot -lt $Arguments.Count; $slot++) {
                if ($children.ContainsKey($slot)) {
                    $child = $children[$slot]
                    if ($child.Exited) {
                        $child.Process.WaitForExit()
                        $code = $child.Process.ExitCode
                        $child.Dispose(); $children.Remove($slot)
                        if ($Once -and $code -ne 0) { throw 'POOL_CHILD_FAILED' }
                        $completed++; $completedSlots[$slot] = $true
                        $nextStart[$slot] = [DateTimeOffset]::UtcNow.AddSeconds(5)
                    }
                }
                if (-not $stopping -and -not $children.ContainsKey($slot) -and
                    -not ($Once -and $completedSlots.ContainsKey($slot)) -and
                    (-not $nextStart.ContainsKey($slot) -or [DateTimeOffset]::UtcNow -ge $nextStart[$slot])) {
                    $children[$slot] = [DocMind.DecryptChild]::new($Executable, $Arguments[$slot])
                    $started++
                }
            }
            $peak = [Math]::Max($peak, $children.Count)
            if (($stopping -and $children.Count -eq 0) -or ($Once -and $completedSlots.Count -eq $Arguments.Count)) { break }
            Start-Sleep -Milliseconds 100
        }
    } finally {
        foreach ($child in $children.Values) {
            try { [void]$child.Stop() } finally { $child.Dispose() }
        }
    }
    return [pscustomobject]@{started=$started;completed=$completed;peak=$peak}
}

function Test-DocMindPoolParent {
    param([int]$ParentProcessId, [string]$ParentStartedUtc)
    $parent = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue
    return $null -ne $parent -and $parent.StartTime.ToUniversalTime().ToString('o') -eq $ParentStartedUtc
}

function ConvertTo-DocMindCommandArgument {
    param([string]$Value)
    # Internal PowerShell arguments may contain spaces, never embedded quotes.
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) { throw 'INVALID_COMMAND_ARGUMENT' }
    return '"' + $Value.TrimEnd('\') + '"'
}
