[CmdletBinding()]
param(
    [string]$ConfigPath = $env:DOCMIND_HOST_WORKER_CONFIG,
    [switch]$Once,
    [ValidateRange(2, 3600)][int]$PollSeconds = 10,
    [switch]$EnableDiscovery,
    [ValidateRange(2, 300)][int]$DiscoverySettleSeconds = 10,
    [ValidateRange(60, 86400)][int]$DiscoveryFallbackSeconds = 3600
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')

if ([string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'DOCMIND_HOST_WORKER_CONFIG or -ConfigPath is required.' }
$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw 'Host worker config file is missing.' }
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($name in @('worker_id', 'api_base_uri', 'key_id', 'shared_secret_file', 'sources')) {
    if ($config.PSObject.Properties.Name -notcontains $name -or $null -eq $config.$name) { throw "Host worker config is missing $name." }
}
Assert-DocMindIdentifier -Value ([string]$config.worker_id) -Name 'worker_id'
Assert-DocMindIdentifier -Value ([string]$config.key_id) -Name 'key_id'
$apiBase = [Uri]::new([string]$config.api_base_uri)
if (-not $apiBase.IsAbsoluteUri -or -not $apiBase.IsLoopback -or @('http', 'https') -notcontains $apiBase.Scheme -or $apiBase.UserInfo) { throw 'api_base_uri must be a loopback HTTP(S) endpoint.' }
$secret = Read-DocMindSharedSecret -LiteralPath ([IO.Path]::GetFullPath([string]$config.shared_secret_file))
$endpoint = '/api/v1/cloud-sync/host-worker/observations'
$deletionEndpoint = '/api/v1/cloud-sync/host-worker/deletions'
$responseNonces = @{}
$reportedDeletions = @{}
$registrations = @()
$sourceRoots = @{}
foreach ($source in @($config.sources)) {
    Assert-DocMindIdentifier -Value ([string]$source.source_id) -Name 'source_id'
    $root = [IO.Path]::GetFullPath([string]$source.root).TrimEnd('\', '/')
    if ($sourceRoots.ContainsKey([string]$source.source_id)) { throw 'Duplicate source_id in host config.' }
    $sourceRoots[[string]$source.source_id] = $root
    if ($source.PSObject.Properties.Name -notcontains 'documents') { continue }
    foreach ($document in @($source.documents)) {
        Assert-DocMindIdentifier -Value ([string]$document.document_id) -Name 'document_id'
        if ([string]::IsNullOrWhiteSpace([string]$document.relative_path)) { throw 'Registered document relative_path is required.' }
        $registrations += [pscustomobject]@{
            source_id = [string]$source.source_id
            root = $root
            document_id = [string]$document.document_id
            relative_path = ([string]$document.relative_path).Replace('\', '/')
        }
    }
}
if ($registrations.Count -eq 0 -and -not $EnableDiscovery) { throw 'No registered documents are configured for observation.' }

$discoveryWatchers = @{}
$discoveryDue = @{}
$discoveryLastAttempt = @{}
$discoveryVerificationPending = @{}
$discoveryEventPrefix = 'DocMindSource-' + [Guid]::NewGuid().ToString('n') + '-'

function Update-DiscoveryWatchers {
    foreach ($sourceId in @($sourceRoots.Keys)) {
        $root = [string]$sourceRoots[$sourceId]
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            if ($discoveryWatchers.ContainsKey($sourceId)) {
                $watcher = $discoveryWatchers[$sourceId]
                $watcher.EnableRaisingEvents = $false
                $watcher.Dispose()
                [void]$discoveryWatchers.Remove($sourceId)
                foreach ($subscriber in @(Get-EventSubscriber | Where-Object { $_.SourceIdentifier.StartsWith(($discoveryEventPrefix + $sourceId + '-'), [StringComparison]::Ordinal) })) {
                    Unregister-Event -SubscriptionId $subscriber.SubscriptionId
                }
            }
            continue
        }
        if ($discoveryWatchers.ContainsKey($sourceId)) { continue }
        $watcher = $null
        try {
            Assert-DocMindNoReparsePoint -LiteralPath $root -Boundary $root -Name 'Source root'
            $watcher = [IO.FileSystemWatcher]::new($root)
            $watcher.IncludeSubdirectories = $true
            $watcher.NotifyFilter = [IO.NotifyFilters]'FileName, DirectoryName, LastWrite, Size, CreationTime'
            $watcher.InternalBufferSize = 65536
            foreach ($eventName in @('Changed', 'Created', 'Deleted', 'Renamed', 'Error')) {
                [void](Register-ObjectEvent -InputObject $watcher -EventName $eventName -SourceIdentifier ($discoveryEventPrefix + $sourceId + '-' + $eventName) -MessageData $sourceId)
            }
            $watcher.EnableRaisingEvents = $true
            $discoveryWatchers[$sourceId] = $watcher
            $discoveryDue[$sourceId] = [DateTimeOffset]::UtcNow.AddSeconds($DiscoverySettleSeconds)
            $discoveryVerificationPending[$sourceId] = $true
        } catch {
            foreach ($subscriber in @(Get-EventSubscriber | Where-Object { $_.SourceIdentifier.StartsWith(($discoveryEventPrefix + $sourceId + '-'), [StringComparison]::Ordinal) })) {
                Unregister-Event -SubscriptionId $subscriber.SubscriptionId
            }
            if ($null -ne $watcher) { $watcher.Dispose() }
            Write-Warning 'Source discovery watcher registration failed; the fallback scan will retry.'
        }
    }
}

function Invoke-DueDiscoveryScans {
    $now = [DateTimeOffset]::UtcNow
    foreach ($eventRecord in @(Get-Event | Where-Object { $_.SourceIdentifier.StartsWith($discoveryEventPrefix, [StringComparison]::Ordinal) })) {
        $sourceId = [string]$eventRecord.MessageData
        if ($sourceRoots.ContainsKey($sourceId)) {
            $discoveryDue[$sourceId] = $now.AddSeconds($DiscoverySettleSeconds)
            $discoveryVerificationPending[$sourceId] = $true
        }
        Remove-Event -EventIdentifier $eventRecord.EventIdentifier
    }
    foreach ($sourceId in @($sourceRoots.Keys)) {
        if (-not $discoveryWatchers.ContainsKey($sourceId)) { continue }
        $fallbackDue = -not $discoveryLastAttempt.ContainsKey($sourceId) -or $now -ge $discoveryLastAttempt[$sourceId].AddSeconds($DiscoveryFallbackSeconds)
        $eventDue = $discoveryDue.ContainsKey($sourceId) -and $now -ge $discoveryDue[$sourceId]
        if (-not $fallbackDue -and -not $eventDue) { continue }
        if ($fallbackDue) { $discoveryVerificationPending[$sourceId] = $true }
        [void]$discoveryDue.Remove($sourceId)
        try {
            & (Join-Path $PSScriptRoot 'Invoke-DocMindSourceReconciliation.ps1') -ConfigPath $configFile -Reason manual -SourceId $sourceId
            if ($discoveryVerificationPending.ContainsKey($sourceId)) {
                [void]$discoveryVerificationPending.Remove($sourceId)
                $discoveryDue[$sourceId] = [DateTimeOffset]::UtcNow.AddSeconds($DiscoverySettleSeconds)
            }
        } catch {
            # A partial scan cannot authorize deletions. Retry after a quiet interval.
            $discoveryDue[$sourceId] = [DateTimeOffset]::UtcNow.AddSeconds($DiscoverySettleSeconds)
            Write-Warning 'Source discovery scan failed; retry is pending.'
        } finally {
            $discoveryLastAttempt[$sourceId] = [DateTimeOffset]::UtcNow
        }
    }
}

function Get-HeaderValue {
    param($Response, [string]$Name)
    $values = $null
    if (-not $Response.Headers.TryGetValues($Name, [ref]$values)) { throw 'OBSERVATION_ACK_UNSIGNED' }
    return [string]@($values)[0]
}

function Send-Observation {
    param($Registration, [IO.FileInfo]$File, [string]$CiphertextSha256)
    $body = [ordered]@{
        source_id = $Registration.source_id
        document_id = $Registration.document_id
        relative_path = $Registration.relative_path
        ciphertext_sha256 = $CiphertextSha256
        size = [Int64]$File.Length
        mtime_ns = ([Int64]$File.LastWriteTimeUtc.Ticks - 621355968000000000L) * 100L
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress))
    $contentHash = Get-DocMindBytesSha256 -Bytes $bytes
    $uri = [Uri]::new($apiBase, $endpoint)
    if (-not $uri.IsLoopback -or $uri.Authority -ne $apiBase.Authority) { throw 'Observation endpoint escaped the configured loopback origin.' }
    $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $nonceBytes = New-DocMindRandomBytes -Count 16
    $nonce = ConvertTo-DocMindHex -Bytes $nonceBytes
    $signature = Get-DocMindHmacSignature -Key $secret -Method 'POST' -PathAndQuery $uri.PathAndQuery -Timestamp $timestamp -Nonce $nonce -ContentSha256 $contentHash
    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, $uri)
    $request.Content = [Net.Http.ByteArrayContent]::new($bytes)
    $request.Content.Headers.ContentType = [Net.Http.Headers.MediaTypeHeaderValue]::new('application/json')
    foreach ($pair in @{
        'X-DocMind-Key-Id' = [string]$config.key_id
        'X-DocMind-Timestamp' = $timestamp
        'X-DocMind-Nonce' = $nonce
        'X-DocMind-Content-SHA256' = $contentHash
        'X-DocMind-Signature' = $signature
    }.GetEnumerator()) { [void]$request.Headers.TryAddWithoutValidation($pair.Key, $pair.Value) }
    $client = [Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    try {
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $responseBytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            $responseTimestamp = Get-HeaderValue -Response $response -Name 'X-DocMind-Timestamp'
            $responseNonce = Get-HeaderValue -Response $response -Name 'X-DocMind-Nonce'
            $responseHash = Get-HeaderValue -Response $response -Name 'X-DocMind-Content-SHA256'
            $responseSignature = Get-HeaderValue -Response $response -Name 'X-DocMind-Signature'
            $responseKeyId = Get-HeaderValue -Response $response -Name 'X-DocMind-Key-Id'
            if ($responseKeyId -ne [string]$config.key_id -or $responseNonces.ContainsKey($responseNonce) -or -not (Test-DocMindSignedMessage -Key $secret -Method 'POST' -PathAndQuery $uri.PathAndQuery -Timestamp $responseTimestamp -Nonce $responseNonce -ContentSha256 $responseHash -Signature $responseSignature -BodyBytes $responseBytes)) {
                throw 'OBSERVATION_ACK_AUTHENTICATION_FAILED'
            }
            $responseNonces[$responseNonce] = [DateTimeOffset]::UtcNow
            foreach ($entry in @($responseNonces.GetEnumerator())) { if ($entry.Value -lt [DateTimeOffset]::UtcNow.AddMinutes(-5)) { $responseNonces.Remove($entry.Key) } }
            if (-not $response.IsSuccessStatusCode) { throw ('OBSERVATION_HTTP_{0}' -f [int]$response.StatusCode) }
        } finally { $response.Dispose() }
    } finally {
        $request.Dispose()
        $client.Dispose()
    }
}

function Send-DeletionObservation {
    param($Registration)
    $body = [ordered]@{
        protocol_version = 1
        worker_id = [string]$config.worker_id
        source_id = $Registration.source_id
        document_id = $Registration.document_id
        relative_path = $Registration.relative_path
        observed_at = [DateTimeOffset]::UtcNow.ToString('o')
        root_access_confirmed = $true
        absence_confirmed = $true
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress))
    $contentHash = Get-DocMindBytesSha256 -Bytes $bytes
    $uri = [Uri]::new($apiBase, $deletionEndpoint)
    if (-not $uri.IsLoopback -or $uri.Authority -ne $apiBase.Authority) { throw 'Deletion endpoint escaped the configured loopback origin.' }
    $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $nonceBytes = New-DocMindRandomBytes -Count 16
    $nonce = ConvertTo-DocMindHex -Bytes $nonceBytes
    $signature = Get-DocMindHmacSignature -Key $secret -Method 'POST' -PathAndQuery $uri.PathAndQuery -Timestamp $timestamp -Nonce $nonce -ContentSha256 $contentHash
    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, $uri)
    $request.Content = [Net.Http.ByteArrayContent]::new($bytes)
    $request.Content.Headers.ContentType = [Net.Http.Headers.MediaTypeHeaderValue]::new('application/json')
    foreach ($pair in @{
        'X-DocMind-Key-Id' = [string]$config.key_id
        'X-DocMind-Timestamp' = $timestamp
        'X-DocMind-Nonce' = $nonce
        'X-DocMind-Content-SHA256' = $contentHash
        'X-DocMind-Signature' = $signature
    }.GetEnumerator()) { [void]$request.Headers.TryAddWithoutValidation($pair.Key, $pair.Value) }
    $client = [Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    try {
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $responseBytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            $responseTimestamp = Get-HeaderValue -Response $response -Name 'X-DocMind-Timestamp'
            $responseNonce = Get-HeaderValue -Response $response -Name 'X-DocMind-Nonce'
            $responseHash = Get-HeaderValue -Response $response -Name 'X-DocMind-Content-SHA256'
            $responseSignature = Get-HeaderValue -Response $response -Name 'X-DocMind-Signature'
            $responseKeyId = Get-HeaderValue -Response $response -Name 'X-DocMind-Key-Id'
            if ($responseKeyId -ne [string]$config.key_id -or $responseNonces.ContainsKey($responseNonce) -or -not (Test-DocMindSignedMessage -Key $secret -Method 'POST' -PathAndQuery $uri.PathAndQuery -Timestamp $responseTimestamp -Nonce $responseNonce -ContentSha256 $responseHash -Signature $responseSignature -BodyBytes $responseBytes)) {
                throw 'DELETION_ACK_AUTHENTICATION_FAILED'
            }
            $responseNonces[$responseNonce] = [DateTimeOffset]::UtcNow
            foreach ($entry in @($responseNonces.GetEnumerator())) { if ($entry.Value -lt [DateTimeOffset]::UtcNow.AddMinutes(-5)) { $responseNonces.Remove($entry.Key) } }
            if (-not $response.IsSuccessStatusCode) { throw ('DELETION_HTTP_{0}' -f [int]$response.StatusCode) }
        } finally { $response.Dispose() }
    } finally {
        $request.Dispose()
        $client.Dispose()
    }
}

try {
do {
    if ($EnableDiscovery) { Update-DiscoveryWatchers }
    $cycleFailures = 0
    foreach ($registration in $registrations) {
        try {
            $sourceFile = Resolve-DocMindSourceFile -Root $registration.root -RelativePath $registration.relative_path
            $before = Get-Item -LiteralPath $sourceFile
            $beforeLength = $before.Length
            $beforeMtime = $before.LastWriteTimeUtc.Ticks
            $hash = Get-DocMindFileSha256 -LiteralPath $sourceFile
            $after = Get-Item -LiteralPath $sourceFile
            if ($after.Length -ne $beforeLength -or $after.LastWriteTimeUtc.Ticks -ne $beforeMtime) { continue }
            if (-not (Test-DocMindFixedTimeHexEqual $hash (Get-DocMindFileSha256 -LiteralPath $sourceFile))) { continue }
            Send-Observation -Registration $registration -File $after -CiphertextSha256 $hash
            [void]$reportedDeletions.Remove(('{0}/{1}' -f $registration.source_id, $registration.document_id))
        } catch {
            $safeCode = $_.Exception.Message
            if ($safeCode -eq 'Encrypted source file is unavailable.') {
                try {
                    if (Test-DocMindConfirmedSourceFileAbsence -Root $registration.root -RelativePath $registration.relative_path) {
                        $deletionKey = '{0}/{1}' -f $registration.source_id, $registration.document_id
                        if (-not $reportedDeletions.ContainsKey($deletionKey)) {
                            Send-DeletionObservation -Registration $registration
                            $reportedDeletions[$deletionKey] = $true
                        }
                        continue
                    }
                } catch { $safeCode = $_.Exception.Message }
            }
            if ($safeCode -notmatch '^[A-Z0-9_]+$' -and -not $safeCode.StartsWith('OBSERVATION_HTTP_')) {
                $innerType = if ($null -ne $_.Exception.InnerException) { $_.Exception.InnerException.GetType().Name.ToUpperInvariant() } else { 'UNKNOWN' }
                $safeCode = 'OBSERVATION_' + $_.Exception.GetType().Name.ToUpperInvariant() + '_' + $innerType
            }
            Write-Warning ("Registered document observation failed: {0}, script_line={1}" -f $safeCode, $_.InvocationInfo.ScriptLineNumber)
            $cycleFailures++
        }
    }
    if ($EnableDiscovery) { Invoke-DueDiscoveryScans }
    if ($Once -and $cycleFailures -gt 0) { throw 'SOURCE_OBSERVATION_CYCLE_FAILED' }
    if (-not $Once) { Start-Sleep -Seconds $PollSeconds }
} while (-not $Once)
} finally {
    foreach ($subscriber in @(Get-EventSubscriber | Where-Object { $_.SourceIdentifier.StartsWith($discoveryEventPrefix, [StringComparison]::Ordinal) })) {
        Unregister-Event -SubscriptionId $subscriber.SubscriptionId
    }
    foreach ($eventRecord in @(Get-Event | Where-Object { $_.SourceIdentifier.StartsWith($discoveryEventPrefix, [StringComparison]::Ordinal) })) {
        Remove-Event -EventIdentifier $eventRecord.EventIdentifier
    }
    foreach ($watcher in @($discoveryWatchers.Values)) { $watcher.EnableRaisingEvents = $false; $watcher.Dispose() }
}
