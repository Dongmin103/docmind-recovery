[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$base=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.local/sync-incremental-tests'))
$testRoot=Join-Path $base ([Guid]::NewGuid().ToString('n'))
[IO.Directory]::CreateDirectory($testRoot)|Out-Null
$source=Join-Path $testRoot 'source';$state=Join-Path $testRoot 'state'
[IO.Directory]::CreateDirectory($source)|Out-Null
[IO.File]::WriteAllText((Join-Path $source 'sample.pdf'),'synthetic encrypted content')
$secret=Join-Path $testRoot 'secret.bin';[IO.File]::WriteAllBytes($secret,[byte[]](0..31))
$listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start();$port=([Net.IPEndPoint]$listener.LocalEndpoint).Port;$listener.Stop()
$configPath=Join-Path $testRoot 'config.json';$log=Join-Path $testRoot 'requests.jsonl'
@{worker_id='test-worker';api_base_uri="http://127.0.0.1:$port";key_id='test-key';shared_secret_file=$secret;sync_state_root=$state;sources=@(@{source_id='test-source';root=$source})}|ConvertTo-Json -Depth 6|Set-Content -LiteralPath $configPath -Encoding UTF8
$python='C:\Users\uplex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if(-not (Test-Path -LiteralPath $python)){$python=(Get-Command python -ErrorAction Stop).Source}
$server=$null;$watcher=$null;$queue=$null
try {
    $server=Start-Process -FilePath $python -ArgumentList @((Join-Path $PSScriptRoot 'test_sync_stub.py'),'--port',$port,'--log',('"'+$log+'"')) -WindowStyle Hidden -PassThru
    $ready=$false
    for($i=0;$i -lt 50;$i++){
        try{$client=[Net.Sockets.TcpClient]::new('127.0.0.1',$port);$client.Dispose();$ready=$true;break}catch{Start-Sleep -Milliseconds 100}
    }
    if(-not $ready){throw 'Synthetic signed endpoint did not start.'}
    $watcher=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $PSScriptRoot 'Watch-DocMindEncryptedSources.ps1')+'"'),'-ConfigPath',('"'+$configPath+'"'),'-SkipStartupScan','-PollSeconds','1') -RedirectStandardOutput (Join-Path $testRoot 'watch.out') -RedirectStandardError (Join-Path $testRoot 'watch.err') -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 2
    Add-Type -Path @((Join-Path $PSScriptRoot 'DocMindSyncQueue.cs'),(Join-Path $PSScriptRoot 'DocMindUnicodeCaseFold.cs'))
    $queue=[DocMind.SyncQueue]::new((Join-Path $state 'sync.sqlite'))
    $null=$queue.Enqueue('test-source','sample.pdf','upsert',$null,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()-121000)
    $until=[DateTime]::UtcNow.AddSeconds(28)
    do {
        if(Test-Path -LiteralPath $log){
            $requests=@(Get-Content -LiteralPath $log | ForEach-Object {$_|ConvertFrom-Json})
            $kinds=@($requests|ForEach-Object{@($_.items|ForEach-Object{$_.kind})})
            if(@($kinds|Where-Object{$_ -eq 'upsert'}).Count -ge 2){break}
        }
        if($watcher.HasExited){throw ('Watcher exited: '+(Get-Content -LiteralPath (Join-Path $testRoot 'watch.err') -Raw))}
        Start-Sleep -Milliseconds 250
    } while([DateTime]::UtcNow -lt $until)
    if(@($kinds|Where-Object{$_ -eq 'dirty'}).Count -ne 1 -or @($kinds|Where-Object{$_ -eq 'upsert'}).Count -ne 2){throw ('Expected dirty + two stable signed upserts; got '+($kinds -join ','))}
    if($queue.Count('test-source') -ne 0){throw 'Acknowledged source was not removed from durable queue.'}
    'PASS: synthetic source -> durable queue -> signed dirty -> two stable signed upserts -> generation-safe ACK.'
} finally {
    if($null -ne $queue){$queue.Dispose()}
    if($null -ne $watcher -and -not $watcher.HasExited){Stop-Process -Id $watcher.Id -Force;$null=$watcher.WaitForExit(5000)}
    if($null -ne $server -and -not $server.HasExited){Stop-Process -Id $server.Id -Force;$null=$server.WaitForExit(5000)}
    if($null -ne $watcher){$watcher.Dispose()};if($null -ne $server){$server.Dispose()}
    if(-not $testRoot.StartsWith($base+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe test cleanup.'}
    if(Test-Path -LiteralPath $testRoot){Remove-Item -LiteralPath $testRoot -Recurse -Force}
}
