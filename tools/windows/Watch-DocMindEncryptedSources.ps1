[CmdletBinding()]
param(
    [string]$ConfigPath = $env:DOCMIND_HOST_WORKER_CONFIG,
    [switch]$Once,
    [ValidateRange(2, 3600)][int]$PollSeconds = 10
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')

if ([string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'DOCMIND_HOST_WORKER_CONFIG or -ConfigPath is required.' }
$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw 'Host worker config file is missing.' }
$config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
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
foreach ($source in @($config.sources)) {
    Assert-DocMindIdentifier -Value ([string]$source.source_id) -Name 'source_id'
    $root = [IO.Path]::GetFullPath([string]$source.root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'A configured source root is unavailable.' }
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
if ($registrations.Count -eq 0) { throw 'No registered documents are configured for observation.' }

function Get-HeaderValue {
    param($Response, [string]$Name)
    $values = $null
    if (-not $Response.Headers.TryGetValues($Name, [ref]$values)) { throw 'OBSERVATION_ACK_UNSIGNED' }
    return [string]@($values)[0]
}

function Send-Observation {
    param($Registration, [IO.FileInfo]$File)
    $body = [ordered]@{
        source_id = $Registration.source_id
        document_id = $Registration.document_id
        relative_path = $Registration.relative_path
        ciphertext_sha256 = Get-DocMindFileSha256 -LiteralPath $File.FullName
        size = [Int64]$File.Length
        mtime_ns = ([Int64]$File.LastWriteTimeUtc.Ticks - 621355968000000000L) * 100L
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress))
    $contentHash = Get-DocMindBytesSha256 -Bytes $bytes
    $uri = [Uri]::new($apiBase, $endpoint)
    if (-not $uri.IsLoopback -or $uri.Authority -ne $apiBase.Authority) { throw 'Observation endpoint escaped the configured loopback origin.' }
    $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $nonceBytes = [byte[]]::new(16)
    [Security.Cryptography.RandomNumberGenerator]::Fill($nonceBytes)
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
    $nonceBytes = [byte[]]::new(16)
    [Security.Cryptography.RandomNumberGenerator]::Fill($nonceBytes)
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

do {
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
            Send-Observation -Registration $registration -File $after
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
            if ($safeCode -notmatch '^[A-Z0-9_]+$' -and -not $safeCode.StartsWith('OBSERVATION_HTTP_')) { $safeCode = 'OBSERVATION_FAILED' }
            Write-Warning ("Registered document observation failed: {0}" -f $safeCode)
        }
    }
    if (-not $Once) { Start-Sleep -Seconds $PollSeconds }
} while (-not $Once)
