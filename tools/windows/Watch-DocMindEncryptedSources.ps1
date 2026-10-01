[CmdletBinding()]
param([string]$ConfigPath=$env:DOCMIND_HOST_WORKER_CONFIG,[switch]$Once,[string]$SourceId,[switch]$SkipStartupScan,[ValidateRange(1,60)][int]$PollSeconds=2)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')
Add-Type -Path @((Join-Path $PSScriptRoot 'DocMindSyncQueue.cs'),(Join-Path $PSScriptRoot 'DocMindUnicodeCaseFold.cs'),(Join-Path $PSScriptRoot 'DocMindSyncCollector.cs'))
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'HOST_CONFIG_REQUIRED' }
$configFile=[IO.Path]::GetFullPath($ConfigPath)
$config=Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
foreach($field in @('worker_id','api_base_uri','key_id','shared_secret_file','sync_state_root','sources')) { if ($config.PSObject.Properties.Name -notcontains $field) { throw "HOST_CONFIG_MISSING_$field" } }
Assert-DocMindIdentifier -Value ([string]$config.worker_id) -Name worker_id
$ownerId='owner-'+[Guid]::NewGuid().ToString('n')
$base=[Uri]::new([string]$config.api_base_uri)
if (-not $base.IsLoopback -or @('http','https') -notcontains $base.Scheme -or $base.UserInfo) { throw 'HOST_API_NOT_LOOPBACK' }
$secret=Read-DocMindSharedSecret -LiteralPath ([IO.Path]::GetFullPath([string]$config.shared_secret_file))
$stateRoot=[IO.Path]::GetFullPath([string]$config.sync_state_root).TrimEnd('\','/')
$roots=@{};$sessions=@{};$renewed=@{};$watchers=@{};$nonces=@{};$scans=@{};$scanRetryAt=@{}
foreach($source in @(Select-DocMindConfiguredSources -Sources @($config.sources) -SourceId $SourceId)) {
    $id=[string]$source.source_id;Assert-DocMindIdentifier -Value $id -Name source_id
    $root=[IO.Path]::GetFullPath([string]$source.root).TrimEnd('\','/')
    if($roots.ContainsKey($id) -or $stateRoot.Equals($root,[StringComparison]::OrdinalIgnoreCase) -or $stateRoot.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'SYNC_STATE_OR_SOURCE_INVALID' }
    $roots[$id]=$root
}
[IO.Directory]::CreateDirectory($stateRoot)|Out-Null
$queue=[DocMind.SyncQueue]::new((Join-Path $stateRoot 'sync.sqlite'))
$sessionUri='/api/v1/cloud-sync/host-worker/changes/session'
$changesUri='/api/v1/cloud-sync/host-worker/changes'

function Send-SignedJson {
    param([string]$Path,[string]$Json)
    $uri=[Uri]::new($base,$Path)
    if(-not $uri.IsLoopback -or $uri.Authority -ne $base.Authority) { throw 'SYNC_ENDPOINT_ESCAPED' }
    $bytes=[Text.Encoding]::UTF8.GetBytes($Json);$hash=Get-DocMindBytesSha256 -Bytes $bytes
    $stamp=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString();$nonce=ConvertTo-DocMindHex -Bytes (New-DocMindRandomBytes -Count 16)
    $sig=Get-DocMindHmacSignature -Key $secret -Method POST -PathAndQuery $uri.PathAndQuery -Timestamp $stamp -Nonce $nonce -ContentSha256 $hash
    $request=[Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post,$uri)
    $request.Content=[Net.Http.ByteArrayContent]::new($bytes)
    $request.Content.Headers.ContentType=[Net.Http.Headers.MediaTypeHeaderValue]::new('application/json')
    foreach($pair in @{'X-DocMind-Key-Id'=[string]$config.key_id;'X-DocMind-Timestamp'=$stamp;'X-DocMind-Nonce'=$nonce;'X-DocMind-Content-SHA256'=$hash;'X-DocMind-Signature'=$sig}.GetEnumerator()) { [void]$request.Headers.TryAddWithoutValidation($pair.Key,$pair.Value) }
    $client=[Net.Http.HttpClient]::new();$client.Timeout=[TimeSpan]::FromSeconds(30)
    try {
        $reply=$client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $data=$reply.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();$headers=@{}
            foreach($name in @('X-DocMind-Key-Id','X-DocMind-Timestamp','X-DocMind-Nonce','X-DocMind-Content-SHA256','X-DocMind-Signature')) {
                $values=$null;if(-not $reply.Headers.TryGetValues($name,[ref]$values)) { throw 'SYNC_RESPONSE_UNSIGNED' };$headers[$name]=[string]@($values)[0]
            }
            if($headers['X-DocMind-Key-Id'] -ne [string]$config.key_id -or $nonces.ContainsKey($headers['X-DocMind-Nonce']) -or -not (Test-DocMindSignedMessage -Key $secret -Method POST -PathAndQuery $uri.PathAndQuery -Timestamp $headers['X-DocMind-Timestamp'] -Nonce $headers['X-DocMind-Nonce'] -ContentSha256 $headers['X-DocMind-Content-SHA256'] -Signature $headers['X-DocMind-Signature'] -BodyBytes $data)) { throw 'SYNC_RESPONSE_AUTH_FAILED' }
            $nonces[$headers['X-DocMind-Nonce']]=[DateTimeOffset]::UtcNow
            foreach($seen in @($nonces.GetEnumerator())) { if($seen.Value -lt [DateTimeOffset]::UtcNow.AddMinutes(-5)) { $nonces.Remove($seen.Key) } }
            $body=if($data.Length) { [Text.Encoding]::UTF8.GetString($data)|ConvertFrom-Json } else { $null }
            return [pscustomobject]@{Status=[int]$reply.StatusCode;Body=$body}
        } finally { $reply.Dispose() }
    } finally { $request.Dispose();$client.Dispose() }
}
function Open-Session {
    param([string]$Id,[switch]$Renew)
    $body=[ordered]@{protocol_version=2;source_id=$Id;worker_id=[string]$config.worker_id;owner_id=$ownerId;action=$(if($Renew){'renew'}else{'acquire'})}
    if($Renew){$body.epoch=[long]$sessions[$Id].epoch}
    $response=Send-SignedJson -Path $sessionUri -Json ($body|ConvertTo-Json -Compress)
    if($response.Status -ne 200){throw ('SESSION_HTTP_{0}_{1}' -f $response.Status,[string]$response.Body.error)}
    $next=$response.Body
    if([string]$next.source_id -ne $Id -or [long]$next.epoch -lt 1 -or (ConvertTo-DocMindDateTimeOffset -Value $next.lease_expires_at) -le [DateTimeOffset]::UtcNow.AddSeconds(30)){throw 'SESSION_RESPONSE_INVALID'}
    if(($sessions.ContainsKey($Id) -and [long]$sessions[$Id].epoch -ne [long]$next.epoch) -or (-not $sessions.ContainsKey($Id) -and ($null -eq $queue.Outbox($Id) -or $queue.Outbox($Id).Epoch -ne [long]$next.epoch))){$queue.ResetForNewEpoch($Id,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())}
    $sessions[$Id]=$next;$renewed[$Id]=[DateTimeOffset]::UtcNow
}
function Launch-Scan {
    param([string]$Id,[string]$Reason)
    if($scans.ContainsKey($Id) -and -not $scans[$Id].HasExited){return $false}
    if($scanRetryAt.ContainsKey($Id) -and [DateTimeOffset]::UtcNow -lt $scanRetryAt[$Id]){return $false}
    $script=Join-Path $PSScriptRoot 'Invoke-DocMindSourceReconciliation.ps1'
    $arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}" -SourceId "{2}" -Reason {3} -OwnerId "{4}" -Epoch {5}' -f $script,$configFile,$Id,$Reason,$ownerId,[long]$sessions[$Id].epoch
    $scans[$Id]=Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $arguments -WindowStyle Hidden -PassThru
    return $true
}
function Process-Scope {
    param([string]$Id)
    foreach($scope in @($queue.Scopes($Id,1))){
        if($scope.Kind -eq 'recovery' -or $scope.Kind -eq 'delete'){if(Launch-Scan -Id $Id -Reason watcher_error){$queue.CompleteScope($scope)};return}
        $relative=[string]$scope.RelativePath
        $directory=[IO.Path]::GetFullPath((Join-Path $roots[$Id] ($relative.Replace('/',[IO.Path]::DirectorySeparatorChar))))
        if(-not $directory.StartsWith($roots[$Id]+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'SCOPE_ESCAPED'}
        if(-not (Test-Path -LiteralPath $directory -PathType Container)){$queue.CompleteScope($scope);return}
        Assert-DocMindNoReparsePoint -LiteralPath $directory -Boundary $roots[$Id] -Name 'Scope directory'
        $prefix=$roots[$Id]+'\'
        foreach($file in [IO.Directory]::EnumerateFiles($directory,'*',[IO.SearchOption]::AllDirectories)){
            Assert-DocMindNoReparsePoint -LiteralPath $file -Boundary $roots[$Id] -Name 'Scope file'
            $path=$file.Substring($prefix.Length).Replace('\','/')
            $old=$null
            if($scope.Kind -eq 'move' -and $scope.OldRelativePath){$old=[string]$scope.OldRelativePath+$path.Substring($relative.Length)}
            $kind=if($old){'move'}else{'upsert'}
            $null=$queue.Enqueue($Id,$path,$kind,$old,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        }
        $queue.CompleteScope($scope)
    }
}
function Send-Outbox {
    param([string]$Id)
    $box=$queue.Outbox($Id)
    if($null -eq $box -or $box.Held -or $box.NextAttemptAt -gt [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()){return}
    if($box.Epoch -ne [long]$sessions[$Id].epoch){$queue.ResetForNewEpoch($Id,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds());return}
    try {
        $response=Send-SignedJson -Path $changesUri -Json $box.Payload
        if($response.Status -ne 200){
            if($response.Status -eq 409 -and [string]$response.Body.error -match '^SESSION_(FENCED|EXPIRED)$'){$sessions.Remove($Id);return}
            $queue.Retry($Id,$box.RequestId,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(),($response.Status -in @(400,401,403,413)));return
        }
        $sent=$box.Payload|ConvertFrom-Json
        if(-not $response.Body.accepted -or [long]$response.Body.epoch -ne $box.Epoch -or [long]$response.Body.sequence -ne $box.Sequence -or @($response.Body.items).Count -ne @($sent.items).Count){throw 'SYNC_ACK_MISMATCH'}
        $outcomes=[Collections.Generic.List[int]]::new()
        for($i=0;$i -lt @($sent.items).Count;$i++){
            $item=$sent.items[$i];$result=$response.Body.items[$i]
            if([string]$item.observation_id -ne [string]$result.observation_id){throw 'SYNC_ACK_MISMATCH'}
            if($item.kind -eq 'dirty'){$outcomes.Add(0)}
            elseif($item.kind -eq 'delete'){$outcomes.Add(2)}
            elseif($item.kind -eq 'move' -and $result.reason -eq 'MOVE_IDENTITY_UNCONFIRMED'){
                $outcomes.Add(2);$now=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                $null=$queue.Enqueue($Id,[string]$item.relative_path,'upsert',$null,$now)
                $null=$queue.Enqueue($Id,[string]$item.old_relative_path,'delete',$null,$now)
            }
            elseif($result.state -eq 'DEFERRED_PREVIOUS_CLEANUP' -or $result.state -eq 'WAITING_SOURCE_STABLE'){$outcomes.Add(1)}
            elseif($result.state -eq 'DEFERRED' -or $result.state -eq 'UNREGISTERED'){$outcomes.Add(3)}
            elseif([string]$item.observation_id -like '*-first-*'){$outcomes.Add(1)}
            else{$outcomes.Add(2)}
        }
        $queue.Acknowledge($Id,$box.RequestId,$outcomes.ToArray(),[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $sessions[$Id].last_sequence=$box.Sequence
    } catch {
        $queue.Retry($Id,$box.RequestId,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(),$false)
        Write-Warning ('SYNC_DELIVERY_RETRY '+$_.Exception.GetType().Name)
    }
}
function Freeze-And-Send {
    param([string]$Id,[DocMind.SyncEntry[]]$Entries,[object[]]$Items)
    $session=$sessions[$Id];$sequence=[long]$session.last_sequence+1;$requestId='change-'+[Guid]::NewGuid().ToString('n')
    $body=[ordered]@{protocol_version=2;source_id=$Id;worker_id=[string]$config.worker_id;owner_id=$ownerId;epoch=[long]$session.epoch;sequence=$sequence;request_id=$requestId;items=$Items}
    $payload=$body|ConvertTo-Json -Compress -Depth 8
    if($queue.Freeze($Id,[long]$session.epoch,$sequence,$requestId,$payload,$Entries)){Send-Outbox -Id $Id}
}
function Read-Fingerprint {
    param([string]$Id,[DocMind.SyncEntry]$Entry)
    $path=Resolve-DocMindSourceFile -Root $roots[$Id] -RelativePath $Entry.RelativePath
    $before=Get-Item -LiteralPath $path -Force;$identity=Get-DocMindHostFileIdentity -LiteralPath $path
    $hash1=Get-DocMindFileSha256 -LiteralPath $path;$hash2=Get-DocMindFileSha256 -LiteralPath $path
    $after=Get-Item -LiteralPath $path -Force;$identity2=Get-DocMindHostFileIdentity -LiteralPath $path
    if($before.Length -ne $after.Length -or $before.LastWriteTimeUtc.Ticks -ne $after.LastWriteTimeUtc.Ticks -or $identity -ne $identity2 -or -not (Test-DocMindFixedTimeHexEqual $hash1 $hash2)){throw 'SOURCE_CHANGED_DURING_HASH'}
    $mtime=([long]$after.LastWriteTimeUtc.Ticks-621355968000000000L)*100L
    $fingerprint=Get-DocMindBytesSha256 -Bytes ([Text.Encoding]::UTF8.GetBytes(('{0}:{1}:{2}:{3}' -f $hash2,[long]$after.Length,$mtime,$identity2)))
    return [pscustomobject]@{Hash=$hash2;Size=[long]$after.Length;Mtime=$mtime;Identity=$identity2;Fingerprint=$fingerprint}
}
function Process-Due {
    param([string]$Id)
    foreach($entry in @($queue.Due($Id,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(),1))){
        try {
            if($entry.Kind -eq 'delete'){
                if(-not (Test-DocMindConfirmedSourceFileAbsence -Root $roots[$Id] -RelativePath $entry.RelativePath)){$null=$queue.Enqueue($Id,$entry.RelativePath,'upsert',$null,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds());return}
                if($entry.StableCount -eq 0){$queue.Delay($entry,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(),2000,1);return}
                $item=[ordered]@{kind='delete';relative_path=$entry.RelativePath;generation=$entry.Generation;observation_id=('obs-'+[Guid]::NewGuid().ToString('n'));root_access_confirmed=$true;absence_confirmed=$true}
            } else {
                $snapshot=Read-Fingerprint -Id $Id -Entry $entry
                if($entry.StableCount -gt 0 -and $entry.Fingerprint -ne $snapshot.Fingerprint){$null=$queue.Enqueue($Id,$entry.RelativePath,'upsert',$entry.OldRelativePath,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds());return}
                if($entry.StableCount -eq 0 -and -not $queue.RememberFingerprint($entry,$snapshot.Fingerprint)){return}
                $stage=if($entry.StableCount -eq 0){'first'}else{'second'}
                $kind=if($entry.Kind -eq 'move' -and $snapshot.Identity){'move'}else{'upsert'}
                $item=[ordered]@{kind=$kind;relative_path=$entry.RelativePath;generation=$entry.Generation;observation_id=('obs-'+$stage+'-'+[Guid]::NewGuid().ToString('n'));ciphertext_sha256=$snapshot.Hash;size=$snapshot.Size;mtime_ns=$snapshot.Mtime}
                if($snapshot.Identity){$item.host_file_id=$snapshot.Identity}
                if($kind -eq 'move'){
                    if(Test-DocMindConfirmedSourceFileAbsence -Root $roots[$Id] -RelativePath $entry.OldRelativePath){$item.old_relative_path=$entry.OldRelativePath;$item.old_absence_confirmed=$true;$item.root_access_confirmed=$true}
                    else{$item.kind='upsert'}
                }
            }
            Freeze-And-Send -Id $Id -Entries ([DocMind.SyncEntry[]]@($entry)) -Items @($item)
        } catch {
            $queue.Delay($entry,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(),5000,$entry.StableCount)
            Write-Warning ('SYNC_FILE_RETRY '+$_.Exception.GetType().Name)
        }
    }
}
try {
    foreach($id in @($roots.Keys)){
        Assert-DocMindNoReparsePoint -LiteralPath $roots[$id] -Boundary $roots[$id] -Name 'Source root'
        $watchers[$id]=[DocMind.SyncCollector]::new($queue,$id,$roots[$id])
    }
    foreach($id in @($roots.Keys)){
        try{Open-Session -Id $id;if(-not $Once -and -not $SkipStartupScan){$null=Launch-Scan -Id $id -Reason startup}}catch{Write-Warning ('SYNC_SESSION_RETRY '+$_.Exception.GetType().Name)}
    }
    $lastMidnight=''
    do {
        foreach($id in @($roots.Keys)){
            try {
                if($scans.ContainsKey($id) -and $scans[$id].HasExited){
                    if($scans[$id].ExitCode -ne 0){$queue.EnqueueScope($id,'',$null,'recovery');$scanRetryAt[$id]=[DateTimeOffset]::UtcNow.AddMinutes(5)}
                    $scans[$id].Dispose();$scans.Remove($id)
                }
                if(-not $sessions.ContainsKey($id)){Open-Session -Id $id}
                elseif([DateTimeOffset]::UtcNow -ge $renewed[$id].AddSeconds(30)){Open-Session -Id $id -Renew}
                Process-Scope -Id $id
                Send-Outbox -Id $id
                if($null -eq $queue.Outbox($id)){
                    $dirty=@($queue.Dirty($id,250))
                    if($dirty.Count){$items=@($dirty|ForEach-Object{[ordered]@{kind='dirty';relative_path=$_.RelativePath;generation=$_.Generation;observation_id=('obs-dirty-'+[Guid]::NewGuid().ToString('n'))}});Freeze-And-Send -Id $id -Entries ([DocMind.SyncEntry[]]$dirty) -Items $items}
                    else{Process-Due -Id $id}
                }
                if($watchers[$id].Faulted){$watchers[$id].Faulted=$false;$queue.EnqueueScope($id,'',$null,'recovery')}
            } catch {
                if($_.Exception.Message -match '^SESSION_HTTP_409_SESSION_(EXPIRED|FENCED)'){$sessions.Remove($id)}
                Write-Warning ('SYNC_SOURCE_RETRY '+$_.Exception.GetType().Name)
            }
        }
        $seoul=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow,'Korea Standard Time')
        $today=$seoul.ToString('yyyy-MM-dd')
        if(-not $Once -and $seoul.Hour -eq 0 -and $lastMidnight -ne $today){foreach($id in @($sessions.Keys)){$null=Launch-Scan -Id $id -Reason scheduled};$lastMidnight=$today}
        if(-not $Once){Start-Sleep -Seconds $PollSeconds}
    } while(-not $Once)
} finally {
    foreach($watcher in @($watchers.Values)){$watcher.Dispose()}
    foreach($scan in @($scans.Values)){$scan.Dispose()}
    $queue.Dispose()
}
