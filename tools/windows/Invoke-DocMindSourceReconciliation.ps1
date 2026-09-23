[CmdletBinding()]
param(
    [string]$ConfigPath = $env:DOCMIND_HOST_WORKER_CONFIG,
    [ValidateSet('startup', 'scheduled', 'manual')][string]$Reason = 'manual',
    [string]$SourceId,
    [ValidateRange(1, 1000)][int]$BatchSize = 250
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
if (-not [string]::IsNullOrWhiteSpace($SourceId)) { Assert-DocMindIdentifier -Value $SourceId -Name 'source_id' }
if ($PSBoundParameters.Keys -notcontains 'BatchSize' -and $config.PSObject.Properties.Name -contains 'scan_batch_size') { $BatchSize = [int]$config.scan_batch_size }
if ($BatchSize -lt 1 -or $BatchSize -gt 1000) { throw 'scan_batch_size must be between 1 and 1000.' }

$apiBase = [Uri]::new([string]$config.api_base_uri)
if (-not $apiBase.IsAbsoluteUri -or -not $apiBase.IsLoopback -or @('http', 'https') -notcontains $apiBase.Scheme -or $apiBase.UserInfo) { throw 'api_base_uri must be a loopback HTTP(S) endpoint.' }
$secret = Read-DocMindSharedSecret -LiteralPath ([IO.Path]::GetFullPath([string]$config.shared_secret_file))
$endpoint = '/api/v1/cloud-sync/host-worker/scans/events'
$claimEndpoint = '/api/v1/cloud-sync/host-worker/reconciliation/claim'
$responseNonces = @{}

function Get-RequiredHeader {
    param($Response, [string]$Name)
    $values = $null
    if (-not $Response.Headers.TryGetValues($Name, [ref]$values)) { throw 'SCAN_ACK_UNSIGNED' }
    return [string]@($values)[0]
}

function Invoke-SignedJsonPost {
    param(
        [Parameter(Mandatory = $true)][string]$RelativeEndpoint,
        [Parameter(Mandatory = $true)]$Body
    )
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 8))
    $contentHash = Get-DocMindBytesSha256 -Bytes $bytes
    $uri = [Uri]::new($apiBase, $RelativeEndpoint)
    if (-not $uri.IsLoopback -or $uri.Authority -ne $apiBase.Authority) { throw 'Scan endpoint escaped the configured loopback origin.' }
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
    $client.Timeout = [TimeSpan]::FromSeconds(60)
    try {
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $responseBytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            $responseTimestamp = Get-RequiredHeader -Response $response -Name 'X-DocMind-Timestamp'
            $responseNonce = Get-RequiredHeader -Response $response -Name 'X-DocMind-Nonce'
            $responseHash = Get-RequiredHeader -Response $response -Name 'X-DocMind-Content-SHA256'
            $responseSignature = Get-RequiredHeader -Response $response -Name 'X-DocMind-Signature'
            $responseKeyId = Get-RequiredHeader -Response $response -Name 'X-DocMind-Key-Id'
            if ($responseKeyId -ne [string]$config.key_id -or $responseNonces.ContainsKey($responseNonce) -or -not (Test-DocMindSignedMessage -Key $secret -Method 'POST' -PathAndQuery $uri.PathAndQuery -Timestamp $responseTimestamp -Nonce $responseNonce -ContentSha256 $responseHash -Signature $responseSignature -BodyBytes $responseBytes)) {
                throw 'SCAN_ACK_AUTHENTICATION_FAILED'
            }
            $responseNonces[$responseNonce] = [DateTimeOffset]::UtcNow
            foreach ($entry in @($responseNonces.GetEnumerator())) { if ($entry.Value -lt [DateTimeOffset]::UtcNow.AddMinutes(-5)) { $responseNonces.Remove($entry.Key) } }
            if (-not $response.IsSuccessStatusCode) { throw ('SCAN_HTTP_{0}' -f [int]$response.StatusCode) }
            if ($responseBytes.Length -eq 0) { return $null }
            return [Text.Encoding]::UTF8.GetString($responseBytes) | ConvertFrom-Json
        } finally { $response.Dispose() }
    } finally {
        $request.Dispose()
        $client.Dispose()
    }
}

function Send-ScanEvent {
    param([Parameter(Mandatory = $true)]$Body)
    [void](Invoke-SignedJsonPost -RelativeEndpoint $endpoint -Body $Body)
}

function New-BaseEvent {
    param([string]$Source, [string]$Scan, [string]$Event)
    return [ordered]@{
        protocol_version = 1
        worker_id = [string]$config.worker_id
        source_id = $Source
        scan_id = $Scan
        event = $Event
        occurred_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
}

function Invoke-SourceScan {
    param(
        [Parameter(Mandatory = $true)]$Source,
        [string]$AssignedScanId,
        [Int64]$ScheduleFencingToken = 0
    )
    $source = $Source
    Assert-DocMindIdentifier -Value ([string]$source.source_id) -Name 'source_id'
    if ($source.PSObject.Properties.Name -notcontains 'root' -or [string]::IsNullOrWhiteSpace([string]$source.root)) { throw 'Every source needs a host-only root.' }
    $sourceKey = [string]$source.source_id
    $scanId = if ([string]::IsNullOrWhiteSpace($AssignedScanId)) { 'scan-{0}' -f [Guid]::NewGuid().ToString('n') } else { $AssignedScanId }
    Assert-DocMindIdentifier -Value $scanId -Name 'scan_id'
    $batchIndex = 0
    $fileCount = 0
    $scanStarted = $false
    try {
        $root = [IO.Path]::GetFullPath([string]$source.root).TrimEnd('\', '/')
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'SOURCE_ROOT_UNAVAILABLE' }
        Assert-DocMindNoReparsePoint -LiteralPath $root -Boundary $root -Name 'Source root'
        try { [void]@(Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop) } catch { throw 'SOURCE_ROOT_ACCESS_FAILED' }
        $started = New-BaseEvent -Source $sourceKey -Scan $scanId -Event 'started'
        $started.reason = $Reason
        $started.root_access_confirmed = $true
        if ($Reason -eq 'scheduled') {
            if ($ScheduleFencingToken -lt 1) { throw 'SIGNED_SCHEDULED_FENCE_INVALID' }
            $started.schedule_fencing_token = $ScheduleFencingToken
        }
        Send-ScanEvent -Body $started
        $scanStarted = $true

        $batch = [Collections.Generic.List[object]]::new()
        Get-DocMindSourceSnapshotEntries -Root $root | ForEach-Object {
            $batch.Add($_)
            $fileCount++
            if ($batch.Count -ge $BatchSize) {
                $message = New-BaseEvent -Source $sourceKey -Scan $scanId -Event 'batch'
                $message.batch_index = $batchIndex
                $message.documents = @($batch)
                Send-ScanEvent -Body $message
                $batchIndex++
                $batch.Clear()
            }
        }
        if ($batch.Count -gt 0) {
            $message = New-BaseEvent -Source $sourceKey -Scan $scanId -Event 'batch'
            $message.batch_index = $batchIndex
            $message.documents = @($batch)
            Send-ScanEvent -Body $message
            $batchIndex++
        }
        $completed = New-BaseEvent -Source $sourceKey -Scan $scanId -Event 'completed'
        $completed.complete = $true
        $completed.file_count = $fileCount
        $completed.batch_count = $batchIndex
        Send-ScanEvent -Body $completed
        Write-Output ("Source reconciliation completed: source_id={0}, scan_id={1}, files={2}, batches={3}." -f $sourceKey, $scanId, $fileCount, $batchIndex)
        return $true
    } catch {
        $errorCode = $_.Exception.Message
        if ($errorCode -notmatch '^[A-Z0-9_]+$' -and -not $errorCode.StartsWith('SCAN_HTTP_')) { $errorCode = 'SOURCE_SCAN_FAILED' }
        try {
            $failed = New-BaseEvent -Source $sourceKey -Scan $scanId -Event 'failed'
            $failed.complete = $false
            $failed.error_code = $errorCode
            $failed.file_count = $fileCount
            $failed.batch_count = $batchIndex
            $failed.root_access_confirmed = $scanStarted
            if ($Reason -eq 'scheduled' -and $ScheduleFencingToken -gt 0) { $failed.schedule_fencing_token = $ScheduleFencingToken }
            Send-ScanEvent -Body $failed
        } catch { Write-Warning 'Source scan failure could not be reported with a signed acknowledgement.' }
        Write-Warning ("Source reconciliation failed: source_id={0}, scan_id={1}, error={2}. No deletion authority was granted." -f $sourceKey, $scanId, $errorCode)
        return $false
    }
}

$configuredSources = @{}
foreach ($configuredSource in @($config.sources)) {
    Assert-DocMindIdentifier -Value ([string]$configuredSource.source_id) -Name 'source_id'
    if ($configuredSources.ContainsKey([string]$configuredSource.source_id)) { throw 'Duplicate source_id in host config.' }
    $configuredSources[[string]$configuredSource.source_id] = $configuredSource
}
if ($configuredSources.Count -eq 0) { throw 'At least one configured source is required.' }
$failedSources = 0
if ($Reason -eq 'scheduled') {
    if (-not [string]::IsNullOrWhiteSpace($SourceId)) { throw 'Scheduled reconciliation source selection is controlled by the signed claim.' }
    $attemptedScheduledScans = @{}
    do {
        $claimBody = [ordered]@{ protocol_version = 1; worker_id = [string]$config.worker_id; lease_seconds = 1800 }
        $claim = Invoke-SignedJsonPost -RelativeEndpoint $claimEndpoint -Body $claimBody
        if ($null -eq $claim -or $null -eq $claim.scan) { break }
        $claimedSourceId = [string]$claim.scan.source_id
        $claimedScanId = [string]$claim.scan.scan_id
        Assert-DocMindIdentifier -Value $claimedSourceId -Name 'source_id'
        Assert-DocMindIdentifier -Value $claimedScanId -Name 'scan_id'
        if ($claimedScanId -notmatch '^midnight-\d{4}-\d{2}-\d{2}$') { throw 'SIGNED_SCHEDULED_SCAN_ID_INVALID' }
        $claimedFence = 0L
        if (-not [Int64]::TryParse([string]$claim.scan.fencing_token, [ref]$claimedFence) -or $claimedFence -lt 1) { throw 'SIGNED_SCHEDULED_FENCE_INVALID' }
        try { $claimedExpiry = ConvertTo-DocMindDateTimeOffset -Value $claim.scan.lease_expires_at -Name 'lease_expires_at' } catch { throw 'SIGNED_SCHEDULED_LEASE_INVALID' }
        if ($claimedExpiry -le [DateTimeOffset]::UtcNow.AddSeconds(5)) { throw 'SIGNED_SCHEDULED_LEASE_INVALID' }
        if (-not $configuredSources.ContainsKey($claimedSourceId)) { throw 'SIGNED_SCHEDULED_SOURCE_UNREGISTERED' }
        $scheduledKey = '{0}/{1}' -f $claimedSourceId, $claimedScanId
        if ($attemptedScheduledScans.ContainsKey($scheduledKey)) { throw 'SIGNED_SCHEDULED_SCAN_REPLAYED' }
        $attemptedScheduledScans[$scheduledKey] = $true
        if (-not (Invoke-SourceScan -Source $configuredSources[$claimedSourceId] -AssignedScanId $claimedScanId -ScheduleFencingToken $claimedFence)) { $failedSources++ }
    } while ($true)
} else {
    $selectedSources = @($config.sources | Where-Object { [string]::IsNullOrWhiteSpace($SourceId) -or [string]$_.source_id -eq $SourceId })
    if ($selectedSources.Count -eq 0) { throw 'No configured source matched the requested source_id.' }
    foreach ($source in $selectedSources) {
        if (-not (Invoke-SourceScan -Source $source)) { $failedSources++ }
    }
}
if ($failedSources -gt 0) { throw 'SOURCE_RECONCILIATION_FAILED' }
