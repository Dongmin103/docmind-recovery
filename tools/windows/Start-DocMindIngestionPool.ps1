[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ConfigPath,
    [ValidateRange(1,12)][int]$Concurrency = 1,
    [Parameter(Mandatory=$true)][string]$StopFile,
    [string]$ClaimSourceId = '', [switch]$SkipRetries, [switch]$Once,
    [string]$TimingRoot = '')
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')
. (Join-Path $PSScriptRoot 'DocMindDecryptGate.ps1')
. (Join-Path $PSScriptRoot 'DocMindIngestionPool.ps1')
$ConfigPath=[IO.Path]::GetFullPath($ConfigPath)
$StopFile=[IO.Path]::GetFullPath($StopFile)
$parentStarted=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
# Own the original role mutex so standalone ingestion cannot overlap the pool.
$singleton=Enter-DocMindWorkerSingleton -Purpose ingest
try {
    $arguments=@()
    for($slot=0;$slot -lt $Concurrency;$slot++) {
        $args='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + (ConvertTo-DocMindCommandArgument (Join-Path $PSScriptRoot 'Start-DocMindUEncryptorHostWorker.ps1'))
        $args+=' -ConfigPath '+(ConvertTo-DocMindCommandArgument $ConfigPath)+' -Purpose ingest -IngestionSlot '+$slot
        $args+=' -PoolSize '+$Concurrency+' -ParentProcessId '+$PID+' -ParentStartedUtc '+(ConvertTo-DocMindCommandArgument $parentStarted)+' -StopFile '+(ConvertTo-DocMindCommandArgument $StopFile)
        if($ClaimSourceId){$args+=' -ClaimSourceId '+(ConvertTo-DocMindCommandArgument $ClaimSourceId)}
        if($SkipRetries){$args+=' -SkipRetries'}
        if($Once){$args+=' -Once'}
        if($TimingRoot){$args+=' -TimingRoot '+(ConvertTo-DocMindCommandArgument (Join-Path $TimingRoot ('slot-'+$slot)))}
        $arguments+=$args
    }
    Invoke-DocMindPool -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments $arguments -StopFile $StopFile -Once:$Once
} finally { $singleton.ReleaseMutex(); $singleton.Dispose() }
