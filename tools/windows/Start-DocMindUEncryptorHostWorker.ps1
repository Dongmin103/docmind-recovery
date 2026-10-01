[CmdletBinding()]
param(
    [string]$ConfigPath = $env:DOCMIND_HOST_WORKER_CONFIG,
    [switch]$Once,
    [switch]$SkipRetries,
    [string]$ClaimSourceId = '',
    [string]$ClaimJobId = '',
    [switch]$PreviewOnly,
    [string]$ClaimFormats = '',
    [int]$MaxPdfPages = 0,
    [string]$CleanupJobId,
    [string]$CleanupVersionId,
    [Int64]$CleanupFencingToken,
    [string]$CleanupPlaintextExtension,
    [switch]$AllowMalformedManifest,
    [string]$TimingRoot = '',
    [ValidateRange(1, 300)][int]$PollSeconds = 5
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module Microsoft.PowerShell.Security -ErrorAction Stop
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')

if ([string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'DOCMIND_HOST_WORKER_CONFIG or -ConfigPath is required.' }
$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw 'Host worker config file is missing.' }
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not [string]::IsNullOrWhiteSpace($CleanupJobId)) {
    Assert-DocMindIdentifier -Value $CleanupJobId -Name 'cleanup_job_id'
    Assert-DocMindIdentifier -Value $CleanupVersionId -Name 'cleanup_version_id'
    if ($CleanupFencingToken -lt 1 -or $CleanupPlaintextExtension -notmatch '^\.(pdf|doc|docx|xlsx|pptx|hwp|hwpx)$' -or $PreviewOnly -or $Once) {
        throw 'TARGET_CLEANUP_ARGUMENTS_INVALID'
    }
} elseif ($CleanupVersionId -or $CleanupFencingToken -or $CleanupPlaintextExtension -or $AllowMalformedManifest) {
    throw 'TARGET_CLEANUP_ARGUMENTS_INVALID'
}
$allowedClaimFormats = @()
if (-not [string]::IsNullOrWhiteSpace($ClaimSourceId)) {
    Assert-DocMindIdentifier -Value $ClaimSourceId -Name 'claim_source_id'
}
if (-not [string]::IsNullOrWhiteSpace($ClaimFormats)) {
    $allowedClaimFormats = @($ClaimFormats.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() })
    $validClaimFormats = @('pdf', 'doc', 'docx', 'xlsx', 'pptx')
    if ($allowedClaimFormats.Count -eq 0 -or @($allowedClaimFormats | Where-Object { $_ -notin $validClaimFormats }).Count -gt 0 -or @($allowedClaimFormats | Select-Object -Unique).Count -ne $allowedClaimFormats.Count) {
        throw 'CLAIM_FORMATS_INVALID'
    }
}
if (-not [string]::IsNullOrWhiteSpace($ClaimJobId) -and
    ($ClaimJobId -cnotmatch '^[0-9a-f]{32}$' -or $ClaimSourceId -ne 'dept-2-e2e' -or
     $allowedClaimFormats.Count -ne 1 -or $allowedClaimFormats[0] -ne 'pptx' -or
     -not $SkipRetries -or -not $Once -or $PreviewOnly -or $CleanupJobId)) {
    throw 'CLAIM_JOB_ID_ARGUMENTS_INVALID'
}
if ($MaxPdfPages -ne 0 -and ($MaxPdfPages -lt 1 -or $MaxPdfPages -gt 30 -or $ClaimSourceId -ne 'dept-2-e2e' -or
    $allowedClaimFormats.Count -ne 1 -or $allowedClaimFormats[0] -ne 'pdf' -or -not $Once -or $PreviewOnly -or $CleanupJobId)) {
    throw 'PDF_PAGE_CAP_ARGUMENTS_INVALID'
}

$requiredConfig = @('worker_id', 'api_base_uri', 'key_id', 'shared_secret_file', 'executable_path', 'executable_sha256', 'executable_signer_thumbprint', 'work_root')
foreach ($name in $requiredConfig) {
    if ($config.PSObject.Properties.Name -notcontains $name -or $null -eq $config.$name -or [string]::IsNullOrWhiteSpace([string]$config.$name)) { throw "Host worker config is missing $name." }
}
if ($config.PSObject.Properties.Name -notcontains 'sources' -or $null -eq $config.sources -or @($config.sources).Count -eq 0) {
    throw 'Host worker config is missing sources.'
}
Assert-DocMindIdentifier -Value ([string]$config.worker_id) -Name 'worker_id'
Assert-DocMindIdentifier -Value ([string]$config.key_id) -Name 'key_id'
if ([string]$config.executable_sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'executable_sha256 is invalid.' }
if ([string]$config.executable_signer_thumbprint -notmatch '^[a-fA-F0-9]{40,64}$') { throw 'executable_signer_thumbprint is invalid.' }

$apiBase = [Uri]::new([string]$config.api_base_uri)
if (-not $apiBase.IsAbsoluteUri -or -not $apiBase.IsLoopback -or @('http', 'https') -notcontains $apiBase.Scheme -or $apiBase.UserInfo) {
    throw 'api_base_uri must be an authenticated loopback HTTP(S) endpoint published by Docker.'
}
$executable = [IO.Path]::GetFullPath([string]$config.executable_path)
$workRoot = [IO.Path]::GetFullPath([string]$config.work_root)
$receiptRoot = if ($config.PSObject.Properties.Name -contains 'cleanup_receipt_root' -and -not [string]::IsNullOrWhiteSpace([string]$config.cleanup_receipt_root)) {
    [IO.Path]::GetFullPath([string]$config.cleanup_receipt_root)
} else { [IO.Path]::GetFullPath(([string]$config.work_root + '-cleanup-receipts')) }
$previewWorkRoot = [IO.Path]::GetFullPath((Join-Path $workRoot 'previews'))
$previewReceiptRoot = [IO.Path]::GetFullPath((Join-Path $receiptRoot 'previews'))
$secretFile = [IO.Path]::GetFullPath([string]$config.shared_secret_file)
if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or [IO.Path]::GetExtension($executable) -ne '.exe') { throw 'Configured executable is unavailable or is not an .exe.' }
if ($executable.Contains('"') -or $workRoot.Contains('"') -or $receiptRoot.Contains('"') -or $previewWorkRoot.Contains('"') -or $previewReceiptRoot.Contains('"')) { throw 'Configured paths containing quotation marks are unsupported.' }

$sources = @{}
foreach ($source in @($config.sources)) {
    if ($source.PSObject.Properties.Name -notcontains 'source_id' -or $source.PSObject.Properties.Name -notcontains 'root') { throw 'Every source needs source_id and root.' }
    Assert-DocMindIdentifier -Value ([string]$source.source_id) -Name 'source_id'
    if ($sources.ContainsKey([string]$source.source_id)) { throw 'Duplicate source_id in host config.' }
    $sourceRoot = [IO.Path]::GetFullPath([string]$source.root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) { throw 'A configured source root is unavailable.' }
    $sources[[string]$source.source_id] = $sourceRoot
}
if ($sources.Count -eq 0) { throw 'At least one source is required.' }
if (-not [string]::IsNullOrWhiteSpace($TimingRoot)) {
    $TimingRoot = [IO.Path]::GetFullPath($TimingRoot)
    if (-not $ClaimSourceId -or $ClaimSourceId -ne 'dept-2-e2e') { throw 'HOST_TIMING_REQUIRES_DEPT2_SCOPE' }
    [IO.Directory]::CreateDirectory($TimingRoot) | Out-Null
    Protect-DocMindJobDirectory -LiteralPath $TimingRoot
}

function Get-HostTimingNanoseconds {
    param([Diagnostics.Stopwatch]$Clock)
    return [Int64][Math]::Round($Clock.ElapsedTicks * (1000000000.0 / [Diagnostics.Stopwatch]::Frequency))
}

function Get-HostTimingSpan {
    param($Start, $End)
    if ($null -eq $Start -or $null -eq $End) { return $null }
    return [Int64]($End - $Start)
}

function Save-HostTiming {
    param($Lease, [string]$Extension, [string]$State, [string]$Code, [string]$StartedUtc, [hashtable]$Marks)
    if ([string]::IsNullOrWhiteSpace($TimingRoot)) { return }
    try {
        $record = [ordered]@{
            job_id = [string]$Lease.job_id
            fencing_token = [Int64]$Lease.fencing_token
            source_id = [string]$Lease.source_id
            extension = $Extension.ToLowerInvariant()
            started_utc = $StartedUtc
            finished_utc = [DateTimeOffset]::UtcNow.ToString('o')
            state = $State
            error_code = $Code
            durations_ns = [ordered]@{
                source_verify = Get-HostTimingSpan $Marks.start $Marks.preflight
                decrypt = Get-HostTimingSpan $Marks.preflight $Marks.decrypt
                post_decrypt_verify = Get-HostTimingSpan $Marks.decrypt $Marks.verify
                delivery_roundtrip = Get-HostTimingSpan $Marks.verify $Marks.delivery
                host_cleanup = Get-HostTimingSpan $Marks.cleanup_start $Marks.cleanup_end
                status_ack = Get-HostTimingSpan $Marks.cleanup_end $Marks.end
                host_total = Get-HostTimingSpan $Marks.start $Marks.end
            }
        }
        $destination = Join-Path $TimingRoot ('{0}-{1}.json' -f $record.job_id, $record.fencing_token)
        $bytes = [Text.Encoding]::UTF8.GetBytes(($record | ConvertTo-Json -Compress -Depth 5) + [Environment]::NewLine)
        $stream = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    } catch {
        Write-Warning 'Host timing record could not be written.'
    }
}
foreach ($sourceRoot in $sources.Values) {
    $sourcePrefix = $sourceRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $workPrefix = $workRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if ($workRoot.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase) -or $workRoot.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or $sourceRoot.StartsWith($workPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'work_root must be separate from every source root.'
    }
    $receiptPrefix = $receiptRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if ($receiptRoot.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase) -or $receiptRoot.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or $sourceRoot.StartsWith($receiptPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'cleanup_receipt_root must be separate from every source root.'
    }
    foreach ($previewRoot in @($previewWorkRoot, $previewReceiptRoot)) {
        $previewPrefix = $previewRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if ($previewRoot.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase) -or $previewRoot.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or $sourceRoot.StartsWith($previewPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Preview roots must be separate from every source root.'
        }
    }
}
$workBoundary = $workRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$receiptBoundary = $receiptRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if ($receiptRoot.Equals($workRoot, [StringComparison]::OrdinalIgnoreCase) -or $receiptRoot.StartsWith($workBoundary, [StringComparison]::OrdinalIgnoreCase) -or $workRoot.StartsWith($receiptBoundary, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'cleanup_receipt_root must be separate from work_root.'
}
if ($config.PSObject.Properties.Name -notcontains 'backup_exclusion_acknowledged' -or $config.backup_exclusion_acknowledged -ne $true) {
    throw 'The plaintext work root backup exclusion must be explicitly acknowledged.'
}
$requireBitLocker = $true
if ($config.PSObject.Properties.Name -contains 'require_bitlocker') { $requireBitLocker = [bool]$config.require_bitlocker }
if ($requireBitLocker) {
    $bitLockerCommand = Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue
    if ($null -eq $bitLockerCommand) { throw 'BITLOCKER_STATUS_UNAVAILABLE' }
    try {
        $workVolume = Get-BitLockerVolume -MountPoint (Split-Path -Qualifier $workRoot) -ErrorAction Stop
    } catch { throw 'BITLOCKER_STATUS_UNAVAILABLE' }
    if ($null -eq $workVolume -or [string]$workVolume.ProtectionStatus -ne 'On') { throw 'BITLOCKER_PROTECTION_REQUIRED' }
}

$timeoutSeconds = 120
if ($config.PSObject.Properties.Name -contains 'job_timeout_seconds') { $timeoutSeconds = [int]$config.job_timeout_seconds }
if ($timeoutSeconds -lt 1 -or $timeoutSeconds -gt 3600) { throw 'job_timeout_seconds must be between 1 and 3600.' }
$leaseSafetySeconds = 10
if ($config.PSObject.Properties.Name -contains 'lease_safety_seconds') { $leaseSafetySeconds = [int]$config.lease_safety_seconds }
if ($leaseSafetySeconds -lt 5 -or $leaseSafetySeconds -gt 300) { throw 'lease_safety_seconds must be between 5 and 300.' }
$reaperMinimumAgeSeconds = 300
if ($config.PSObject.Properties.Name -contains 'reaper_minimum_age_seconds') { $reaperMinimumAgeSeconds = [int]$config.reaper_minimum_age_seconds }
$claimLeaseSeconds = 1800
if ($config.PSObject.Properties.Name -contains 'claim_lease_seconds') { $claimLeaseSeconds = [int]$config.claim_lease_seconds }
if ($claimLeaseSeconds -lt 30 -or $claimLeaseSeconds -gt 1800) { throw 'claim_lease_seconds must be between 30 and 1800.' }
$cleanupReceiptReplaySeconds = 60
if ($config.PSObject.Properties.Name -contains 'cleanup_receipt_replay_seconds') { $cleanupReceiptReplaySeconds = [int]$config.cleanup_receipt_replay_seconds }
if ($cleanupReceiptReplaySeconds -lt 30 -or $cleanupReceiptReplaySeconds -gt 3600) { throw 'cleanup_receipt_replay_seconds must be between 30 and 3600.' }

$secret = Read-DocMindSharedSecret -LiteralPath $secretFile
$expectedExecutableHash = ([string]$config.executable_sha256).ToLowerInvariant()
$expectedSignerThumbprint = (([string]$config.executable_signer_thumbprint) -replace '\s', '').ToUpperInvariant()
$claimPath = '/api/v1/cloud-sync/host-worker/claim'
$jobsPath = '/api/v1/cloud-sync/host-worker/jobs'
$previewClaimPath = '/api/v1/cloud-sync/host-worker/preview/claim'
$previewJobsPath = '/api/v1/cloud-sync/host-worker/previews'
$previewEnabled = $false
if ($config.PSObject.Properties.Name -contains 'preview_enabled') { $previewEnabled = [bool]$config.preview_enabled }
if ($PreviewOnly -and -not $previewEnabled) { throw 'PREVIEW_NOT_ENABLED' }
$responseNonces = @{}
$lastFencingTokenByJob = @{}

function Assert-ExecutableIdentity {
    $hash = Get-DocMindFileSha256 -LiteralPath $executable
    if (-not (Test-DocMindFixedTimeHexEqual $hash $expectedExecutableHash)) { throw 'EXECUTABLE_FINGERPRINT_CHANGED' }
    $signature = Get-AuthenticodeSignature -LiteralPath $executable
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or $null -eq $signature.SignerCertificate) { throw 'EXECUTABLE_SIGNATURE_INVALID' }
    $actualThumbprint = ($signature.SignerCertificate.Thumbprint -replace '\s', '').ToUpperInvariant()
    if ($actualThumbprint -ne $expectedSignerThumbprint) { throw 'EXECUTABLE_SIGNER_CHANGED' }
    return $hash
}

function New-SignedRequestHeaders {
    param([string]$Method, [Uri]$Uri, [string]$ContentSha256)
    $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $nonceBytes = New-DocMindRandomBytes -Count 16
    $nonce = ConvertTo-DocMindHex -Bytes $nonceBytes
    $signature = Get-DocMindHmacSignature -Key $secret -Method $Method -PathAndQuery $Uri.PathAndQuery -Timestamp $timestamp -Nonce $nonce -ContentSha256 $ContentSha256
    return @{
        'X-DocMind-Key-Id' = [string]$config.key_id
        'X-DocMind-Timestamp' = $timestamp
        'X-DocMind-Nonce' = $nonce
        'X-DocMind-Content-SHA256' = $ContentSha256
        'X-DocMind-Signature' = $signature
    }
}

function Get-RequiredResponseHeader {
    param($Response, [string]$Name)
    $values = $null
    if (-not $Response.Headers.TryGetValues($Name, [ref]$values)) { throw 'API_RESPONSE_UNSIGNED' }
    return [string]@($values)[0]
}

function Assert-SignedResponse {
    param($Response, [string]$Method, [Uri]$Uri, [byte[]]$BodyBytes)
    $timestamp = Get-RequiredResponseHeader -Response $Response -Name 'X-DocMind-Timestamp'
    $nonce = Get-RequiredResponseHeader -Response $Response -Name 'X-DocMind-Nonce'
    $contentHash = Get-RequiredResponseHeader -Response $Response -Name 'X-DocMind-Content-SHA256'
    $signature = Get-RequiredResponseHeader -Response $Response -Name 'X-DocMind-Signature'
    $keyId = Get-RequiredResponseHeader -Response $Response -Name 'X-DocMind-Key-Id'
    if ($keyId -ne [string]$config.key_id -or $responseNonces.ContainsKey($nonce)) { throw 'API_RESPONSE_AUTHENTICATION_FAILED' }
    if (-not (Test-DocMindSignedMessage -Key $secret -Method $Method -PathAndQuery $Uri.PathAndQuery -Timestamp $timestamp -Nonce $nonce -ContentSha256 $contentHash -Signature $signature -BodyBytes $BodyBytes)) {
        throw 'API_RESPONSE_AUTHENTICATION_FAILED'
    }
    $responseNonces[$nonce] = [DateTimeOffset]::UtcNow
    foreach ($entry in @($responseNonces.GetEnumerator())) { if ($entry.Value -lt [DateTimeOffset]::UtcNow.AddMinutes(-5)) { $responseNonces.Remove($entry.Key) } }
}

function Invoke-SignedBytesRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$RelativeUri,
        [Parameter(Mandatory = $true)][string]$ContentSha256,
        [Parameter(Mandatory = $true)]$Content,
        [Parameter(Mandatory = $true)][string]$ContentType,
        [hashtable]$AdditionalHeaders = @{},
        [ValidateRange(1, 3600)][int]$RequestTimeoutSeconds = 30
    )
    $uri = [Uri]::new($apiBase, $RelativeUri)
    if (-not $uri.IsLoopback -or $uri.Authority -ne $apiBase.Authority) { throw 'API request escaped the configured loopback origin.' }
    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::new($Method), $uri)
    $request.Content = $Content
    $request.Content.Headers.ContentType = [Net.Http.Headers.MediaTypeHeaderValue]::new($ContentType)
    foreach ($entry in (New-SignedRequestHeaders -Method $Method -Uri $uri -ContentSha256 $ContentSha256).GetEnumerator()) { [void]$request.Headers.TryAddWithoutValidation($entry.Key, $entry.Value) }
    foreach ($entry in $AdditionalHeaders.GetEnumerator()) { [void]$request.Headers.TryAddWithoutValidation($entry.Key, [string]$entry.Value) }
    $client = [Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromSeconds($RequestTimeoutSeconds)
    try {
        $response = $client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        try {
            $responseBytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            Assert-SignedResponse -Response $response -Method $Method -Uri $uri -BodyBytes $responseBytes
            if (-not $response.IsSuccessStatusCode) {
                $safeError = $null
                try {
                    $errorEnvelope = [Text.Encoding]::UTF8.GetString($responseBytes) | ConvertFrom-Json
                    if ([string]$errorEnvelope.error -match '^[A-Z0-9_]{1,64}$') {
                        $safeError = [string]$errorEnvelope.error
                    }
                } catch { $safeError = $null }
                if ($safeError) { throw $safeError }
                throw ('API_HTTP_{0}' -f [int]$response.StatusCode)
            }
            return [pscustomobject]@{ StatusCode = [int]$response.StatusCode; Body = $responseBytes }
        } finally { $response.Dispose() }
    } finally {
        $request.Dispose()
        $client.Dispose()
    }
}

function Invoke-SignedJsonRequest {
    param([string]$Method, [string]$RelativeUri, $Body)
    $json = $Body | ConvertTo-Json -Compress -Depth 6
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $content = [Net.Http.ByteArrayContent]::new($bytes)
    return Invoke-SignedBytesRequest -Method $Method -RelativeUri $RelativeUri -ContentSha256 (Get-DocMindBytesSha256 -Bytes $bytes) -Content $content -ContentType 'application/json'
}

function Send-WorkerStatus {
    param($Lease, [string]$State, [string]$ErrorCode, [bool]$CleanupComplete, [ValidateSet('ingest', 'preview')][string]$Purpose = 'ingest')
    $status = if ($State -eq 'COMPLETE' -and $CleanupComplete) { 'CLEANED' } elseif (-not $CleanupComplete) { 'CLEANUP_FAILED' } else { 'FAILED' }
    $body = [ordered]@{
        worker_id = [string]$config.worker_id
        version_id = [string]$Lease.version_id
        fencing_token = [Int64]$Lease.fencing_token
        status = $status
        error_code = $ErrorCode
    }
    Send-WorkerStatusBody -JobId ([string]$Lease.job_id) -Body $body -Purpose $Purpose
}

function Send-WorkerStatusBody {
    param([string]$JobId, $Body, [ValidateSet('ingest', 'preview')][string]$Purpose = 'ingest')
    $path = if ($Purpose -eq 'preview') { $previewJobsPath } else { $jobsPath }
    $result = Invoke-SignedJsonRequest -Method 'POST' -RelativeUri ("$path/$([Uri]::EscapeDataString($JobId))/status") -Body $Body
    if ($result.Body.Length -eq 0) { throw 'STATUS_ACK_MISSING' }
    $ack = [Text.Encoding]::UTF8.GetString($result.Body) | ConvertFrom-Json
    if ($ack.accepted -ne $true -or [string]$ack.job_id -ne $JobId -or [string]$ack.version_id -ne [string]$Body.version_id -or [Int64]$ack.fencing_token -ne [Int64]$Body.fencing_token) { throw 'STATUS_ACK_MISMATCH' }
}

function Send-CleanupReceipt {
    param($Receipt, [ValidateSet('ingest', 'preview')][string]$Purpose = 'ingest')
    $body = [ordered]@{
        worker_id = [string]$config.worker_id
        version_id = [string]$Receipt.version_id
        fencing_token = [Int64]$Receipt.fencing_token
        status = [string]$Receipt.final_state
        error_code = $Receipt.error_code
    }
    Send-WorkerStatusBody -JobId ([string]$Receipt.job_id) -Body $body -Purpose $Purpose
}

function Invoke-TargetHostCleanup {
    $prepareBody = [ordered]@{
        worker_id = [string]$config.worker_id
        version_id = $CleanupVersionId
        fencing_token = $CleanupFencingToken
    }
    $relativeUri = "$jobsPath/$([Uri]::EscapeDataString($CleanupJobId))/cleanup/prepare"
    $response = Invoke-SignedJsonRequest -Method 'POST' -RelativeUri $relativeUri -Body $prepareBody
    $prepared = [Text.Encoding]::UTF8.GetString($response.Body) | ConvertFrom-Json
    if ([string]$prepared.job_id -ne $CleanupJobId -or [string]$prepared.version_id -ne $CleanupVersionId -or
        [Int64]$prepared.fencing_token -ne $CleanupFencingToken -or [Int64]$prepared.plaintext_size -lt 1) {
        throw 'TARGET_CLEANUP_PREPARE_MISMATCH'
    }
    $expiry = ConvertTo-DocMindDateTimeOffset -Value ([string]$prepared.lease_expires_at) -Name 'lease_expires_at'
    if ($expiry -ge [DateTimeOffset]::UtcNow) { throw 'TARGET_CLEANUP_LEASE_ACTIVE' }
    Assert-DocMindNoReparsePoint -LiteralPath $workRoot -Boundary $workRoot -Name 'Work root'
    $directories = @(Get-ChildItem -LiteralPath $workRoot -Directory -Filter "$CleanupJobId-*" -Force -ErrorAction Stop)
    if ($directories.Count -ne 1 -or $directories[0].Name -notmatch ('^{0}-[0-9a-f]{{32}}$' -f [regex]::Escape($CleanupJobId))) {
        throw 'TARGET_CLEANUP_DIRECTORY_MISMATCH'
    }
    $directory = $directories[0]
    Assert-DocMindNoReparsePoint -LiteralPath $directory.FullName -Boundary $workRoot -Name 'Cleanup target'
    if (@(Get-ChildItem -LiteralPath $directory.FullName -Directory -Force -ErrorAction Stop).Count -ne 0) {
        throw 'TARGET_CLEANUP_DIRECTORY_MISMATCH'
    }
    $files = @(Get-ChildItem -LiteralPath $directory.FullName -File -Force -ErrorAction Stop)
    $plainName = 'plaintext' + $CleanupPlaintextExtension.ToLowerInvariant()
    if ($files.Count -ne 2 -or @($files | Where-Object { $_.Name -notin @('job-state.json', $plainName) }).Count -ne 0) {
        throw 'TARGET_CLEANUP_FILE_MISMATCH'
    }
    $statePath = Join-Path $directory.FullName 'job-state.json'
    $plainPath = Join-Path $directory.FullName $plainName
    Assert-DocMindNoReparsePoint -LiteralPath $statePath -Boundary $workRoot -Name 'Job state'
    Assert-DocMindNoReparsePoint -LiteralPath $plainPath -Boundary $workRoot -Name 'Plaintext target'
    if ((Get-Item -LiteralPath $plainPath).Length -ne [Int64]$prepared.plaintext_size) {
        throw 'TARGET_CLEANUP_SIZE_MISMATCH'
    }
    $rawState = [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8)
    $stateRecord = $rawState | ConvertFrom-Json
    if ($stateRecord -is [string]) {
        if (-not $AllowMalformedManifest -or $stateRecord -ne 'System.Collections.Specialized.OrderedDictionary') {
            throw 'TARGET_CLEANUP_MANIFEST_INVALID'
        }
    } elseif ([string]$stateRecord.job_id -ne $CleanupJobId -or
        [string]$stateRecord.version_id -ne $CleanupVersionId -or
        [Int64]$stateRecord.fencing_token -ne $CleanupFencingToken -or
        [string]$stateRecord.state -ne 'ACKNOWLEDGED' -or
        (ConvertTo-DocMindDateTimeOffset -Value ([string]$stateRecord.lease_expires_at) -Name 'lease_expires_at') -ne $expiry) {
        throw 'TARGET_CLEANUP_MANIFEST_MISMATCH'
    }
    $exclusive = [IO.File]::Open($plainPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $exclusive.Dispose()
    [void](Write-DocMindCleanupReceiptAtomic -ReceiptRoot $receiptRoot -JobId $CleanupJobId -VersionId $CleanupVersionId -FencingToken $CleanupFencingToken -FinalState 'CLEANUP_PENDING' -ErrorCode $null)
    Remove-Item -LiteralPath $directory.FullName -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $directory.FullName) { throw 'TARGET_CLEANUP_DELETE_FAILED' }
    $receiptPath = Write-DocMindCleanupReceiptAtomic -ReceiptRoot $receiptRoot -JobId $CleanupJobId -VersionId $CleanupVersionId -FencingToken $CleanupFencingToken -FinalState 'CLEANED' -ErrorCode $null
    $receipt = Read-DocMindCleanupReceipt -LiteralPath $receiptPath -ReceiptRoot $receiptRoot
    Send-CleanupReceipt -Receipt $receipt
    Remove-Item -LiteralPath $receiptPath -Force -ErrorAction Stop
    Write-Output "Target host cleanup acknowledged: $CleanupJobId"
}

function Stop-WorkerProcessTree {
    param([Diagnostics.Process]$Process)
    if ($null -eq $Process -or $Process.HasExited) { return }
    & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
    [void]$Process.WaitForExit(5000)
    if (-not $Process.HasExited) {
        try { $Process.Kill() } catch { }
        if (-not $Process.WaitForExit(5000)) { throw 'PROCESS_TERMINATION_FAILED' }
    }
}

function Invoke-LeasedDecryptJob {
    param($Lease, [ValidateSet('ingest', 'preview')][string]$Purpose = 'ingest')
    $hostClock = [Diagnostics.Stopwatch]::StartNew()
    $hostStartedUtc = [DateTimeOffset]::UtcNow.ToString('o')
    $marks = @{ start = [Int64]0; preflight = $null; decrypt = $null; verify = $null; delivery = $null; cleanup_start = $null; cleanup_end = $null; end = $null }
    if ($Purpose -eq 'preview') {
        $workRoot = $previewWorkRoot
        $receiptRoot = $previewReceiptRoot
        $jobsPath = $previewJobsPath
    }
    $validationLease = if ($Purpose -eq 'preview') { $Lease | Select-Object job_id, source_id, document_id, version_id, relative_path, ciphertext_sha256, fencing_token, lease_expires_at } else { $Lease }
    $leaseExpiry = Assert-DocMindLeasePayload -Payload $validationLease -MinimumRemainingSeconds $leaseSafetySeconds
    if (-not $sources.ContainsKey([string]$Lease.source_id)) { throw 'SOURCE_NOT_REGISTERED' }
    $sourceFile = Resolve-DocMindSourceFile -Root $sources[[string]$Lease.source_id] -RelativePath ([string]$Lease.relative_path)
    $sourceHashBefore = Get-DocMindFileSha256 -LiteralPath $sourceFile
    if (-not (Test-DocMindFixedTimeHexEqual $sourceHashBefore ([string]$Lease.ciphertext_sha256).ToLowerInvariant())) { throw 'SOURCE_FINGERPRINT_MISMATCH' }
    if ($Purpose -eq 'preview' -and $Lease.PSObject.Properties.Name -contains 'ciphertext_size' -and (Get-Item -LiteralPath $sourceFile).Length -ne [Int64]$Lease.ciphertext_size) { throw 'SOURCE_FINGERPRINT_MISMATCH' }
    $executableHashBefore = Assert-ExecutableIdentity
    $marks.preflight = Get-HostTimingNanoseconds $hostClock

    [IO.Directory]::CreateDirectory($workRoot) | Out-Null
    Assert-DocMindNoReparsePoint -LiteralPath $workRoot -Boundary $workRoot -Name 'Work root'
    $jobDirectory = Join-Path $workRoot (('{0}-{1}' -f [string]$Lease.job_id, [Guid]::NewGuid().ToString('n')))
    $outputExtension = [IO.Path]::GetExtension([string]$Lease.relative_path)
    if ($outputExtension.Length -gt 16 -or $outputExtension -notmatch '^\.[A-Za-z0-9]+$') { $outputExtension = '.bin' }
    $outputFile = Join-Path $jobDirectory ('plaintext' + $outputExtension.ToLowerInvariant())
    $process = $null
    $mutex = $null
    $mutexAcquired = $false
    $cleanupComplete = $false
    $jobSucceeded = $false
    $errorCode = $null
    $receiptPath = $null
    try {
        [IO.Directory]::CreateDirectory($jobDirectory) | Out-Null
        Protect-DocMindJobDirectory -LiteralPath $jobDirectory
        Write-JobState -JobDirectory $jobDirectory -Lease $Lease -State 'DECRYPTING'
        $createdNew = $false
        $mutex = [Threading.Mutex]::new($false, 'Global\DocMind-uEncryptor2-SingleRun', [ref]$createdNew)
        $mutexAcquired = $mutex.WaitOne([TimeSpan]::FromSeconds(5))
        if (-not $mutexAcquired) { throw 'HOST_CONCURRENCY_LIMIT' }

        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $executable
        $startInfo.Arguments = ('"{0}" "{1}" /dec' -f $sourceFile, $outputFile)
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardInput = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw 'PROCESS_START_FAILED' }
        $process.StandardInput.Close()
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($timeoutSeconds * 1000)) { Stop-WorkerProcessTree -Process $process; throw 'TIMEOUT_OR_INTERACTIVE_PROMPT' }
        [void]$stdoutTask.GetAwaiter().GetResult()
        [void]$stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'NONZERO_EXIT' }
        if (-not (Test-Path -LiteralPath $outputFile -PathType Leaf)) { throw 'OUTPUT_MISSING' }
        $outputItem = Get-Item -LiteralPath $outputFile
        if ($outputItem.Length -le 0) { throw 'OUTPUT_EMPTY' }
        if ($Purpose -eq 'preview' -and $outputItem.Length -gt 64MB) { throw 'PREVIEW_INPUT_TOO_LARGE' }
        $marks.decrypt = Get-HostTimingNanoseconds $hostClock

        $sourceHashAfter = Get-DocMindFileSha256 -LiteralPath $sourceFile
        $executableHashAfter = Assert-ExecutableIdentity
        if (-not (Test-DocMindFixedTimeHexEqual $sourceHashBefore $sourceHashAfter) -or -not (Test-DocMindFixedTimeHexEqual $executableHashBefore $executableHashAfter)) { throw 'INPUT_FINGERPRINT_CHANGED' }
        if ([DateTimeOffset]::UtcNow.AddSeconds($leaseSafetySeconds) -ge $leaseExpiry) { throw 'LEASE_EXPIRED_BEFORE_DELIVERY' }

        Write-JobState -JobDirectory $jobDirectory -Lease $Lease -State 'DELIVERING'
        $plainHash = Get-DocMindFileSha256 -LiteralPath $outputFile
        $marks.verify = Get-HostTimingNanoseconds $hostClock
        $stream = [IO.File]::Open($outputFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        try {
            $content = New-DocMindArtifactStreamContent -Stream $stream -Length $outputItem.Length
            $relativeUri = "$jobsPath/$([Uri]::EscapeDataString([string]$Lease.job_id))/artifact"
            if ($MaxPdfPages -gt 0) { $relativeUri += "?max_pdf_pages=$MaxPdfPages" }
            $artifactHeaders = @{
                'X-DocMind-Worker-Id' = [string]$config.worker_id
                'X-DocMind-Version-Id' = [string]$Lease.version_id
                'X-DocMind-Fencing-Token' = [string][Int64]$Lease.fencing_token
                'X-DocMind-Plaintext-SHA256' = $plainHash
                'X-DocMind-Plaintext-Size' = [string]$outputItem.Length
            }
            $artifactTimeoutSeconds = Get-DocMindArtifactRequestTimeoutSeconds -LeaseExpiresAt $leaseExpiry -SafetySeconds 5
            if ($Purpose -eq 'preview') { $artifactTimeoutSeconds = [Math]::Min(120, $artifactTimeoutSeconds) }
            $result = Invoke-SignedBytesRequest -Method 'PUT' -RelativeUri $relativeUri -ContentSha256 $plainHash -Content $content -ContentType 'application/octet-stream' -AdditionalHeaders $artifactHeaders -RequestTimeoutSeconds $artifactTimeoutSeconds
        } finally { $stream.Dispose() }
        if ($result.Body.Length -eq 0) { throw 'DELIVERY_ACK_MISSING' }
        $ack = [Text.Encoding]::UTF8.GetString($result.Body) | ConvertFrom-Json
        if ($ack.accepted -ne $true -or [string]$ack.job_id -ne [string]$Lease.job_id -or [string]$ack.version_id -ne [string]$Lease.version_id -or [Int64]$ack.fencing_token -ne [Int64]$Lease.fencing_token) { throw 'DELIVERY_ACK_MISMATCH' }
        if ($Purpose -eq 'preview' -and $ack.cleanup_required -ne $true) { throw 'DELIVERY_ACK_MISMATCH' }
        $jobSucceeded = $true
        $marks.delivery = Get-HostTimingNanoseconds $hostClock
        Write-JobState -JobDirectory $jobDirectory -Lease $Lease -State 'ACKNOWLEDGED'
    } catch {
        $errorCode = $_.Exception.Message
        if ($errorCode -notmatch '^[A-Z0-9_]+$') { $errorCode = 'HOST_WORKER_ERROR' }
    } finally {
        $marks.cleanup_start = Get-HostTimingNanoseconds $hostClock
        if ($process) {
            try { Stop-WorkerProcessTree -Process $process } catch { $errorCode = 'PROCESS_TERMINATION_FAILED' }
            $process.Dispose()
        }
        if ($mutexAcquired) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
        if (Test-Path -LiteralPath $jobDirectory) {
            try {
                $receiptPath = Write-DocMindCleanupReceiptAtomic -ReceiptRoot $receiptRoot -JobId ([string]$Lease.job_id) -VersionId ([string]$Lease.version_id) -FencingToken ([Int64]$Lease.fencing_token) -FinalState 'CLEANUP_PENDING' -ErrorCode $null
            } catch { $errorCode = 'CLEANUP_RECEIPT_WRITE_FAILED'; $jobSucceeded = $false }
            try {
                Assert-DocMindNoReparsePoint -LiteralPath $jobDirectory -Boundary $workRoot -Name 'Cleanup target'
                Remove-Item -LiteralPath $jobDirectory -Recurse -Force -ErrorAction Stop
            } catch { $errorCode = 'PLAINTEXT_CLEANUP_FAILED' }
        }
        $cleanupComplete = -not (Test-Path -LiteralPath $jobDirectory)
        if (-not $cleanupComplete) { $errorCode = 'PLAINTEXT_CLEANUP_FAILED'; $jobSucceeded = $false }
        $marks.cleanup_end = Get-HostTimingNanoseconds $hostClock
    }
    $finalState = if ($jobSucceeded -and $cleanupComplete) { 'COMPLETE' } elseif (-not $cleanupComplete) { 'CLEANUP_FAILED' } else { 'FAILED' }
    if ($cleanupComplete) {
        $receiptFinalState = if ($finalState -eq 'COMPLETE') { 'CLEANED' } else { 'FAILED' }
        try {
            $receiptPath = Write-DocMindCleanupReceiptAtomic -ReceiptRoot $receiptRoot -JobId ([string]$Lease.job_id) -VersionId ([string]$Lease.version_id) -FencingToken ([Int64]$Lease.fencing_token) -FinalState $receiptFinalState -ErrorCode $errorCode
        } catch { $jobSucceeded = $false; $errorCode = 'CLEANUP_RECEIPT_WRITE_FAILED' }
    }
    try {
        Send-WorkerStatus -Lease $Lease -State $finalState -ErrorCode $errorCode -CleanupComplete $cleanupComplete -Purpose $Purpose
        if ($cleanupComplete -and $null -ne $receiptPath -and (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
            Assert-DocMindNoReparsePoint -LiteralPath $receiptPath -Boundary $receiptRoot -Name 'Acknowledged cleanup receipt'
            Remove-Item -LiteralPath $receiptPath -Force -ErrorAction Stop
        }
    } catch { if ($jobSucceeded) { $jobSucceeded = $false; $errorCode = 'STATUS_ACK_FAILED' } }
    $marks.end = Get-HostTimingNanoseconds $hostClock
    Save-HostTiming -Lease $Lease -Extension $outputExtension -State $finalState -Code $errorCode -StartedUtc $hostStartedUtc -Marks $marks
    if (-not $jobSucceeded) { throw $errorCode }
}

function ConvertTo-PreviewLease {
    param([Parameter(Mandatory = $true)]$Claim)
    if ($Claim.PSObject.Properties.Name -notcontains 'id' -or [string]$Claim.id -ne [string]$Claim.job_id) { throw 'PREVIEW_CLAIM_ID_MISMATCH' }
    if ($Claim.PSObject.Properties.Name -notcontains 'ciphertext_size') { throw 'PREVIEW_CLAIM_SIZE_MISSING' }
    $sourceSize = 0L
    if (-not [Int64]::TryParse([string]$Claim.ciphertext_size, [ref]$sourceSize) -or $sourceSize -lt 1) { throw 'PREVIEW_CLAIM_SIZE_INVALID' }
    $lease = [pscustomobject]@{
        job_id = [string]$Claim.job_id
        source_id = [string]$Claim.source_id
        document_id = [string]$Claim.document_id
        version_id = [string]$Claim.version_id
        relative_path = [string]$Claim.relative_path
        ciphertext_sha256 = [string]$Claim.ciphertext_sha256
        fencing_token = $Claim.fencing_token
        lease_expires_at = $Claim.lease_expires_at
    }
    [void](Assert-DocMindLeasePayload -Payload $lease -MinimumRemainingSeconds $leaseSafetySeconds)
    $lease | Add-Member -NotePropertyName ciphertext_size -NotePropertyValue $sourceSize
    return $lease
}

[void](Assert-ExecutableIdentity)
if (-not [string]::IsNullOrWhiteSpace($CleanupJobId)) {
    Invoke-TargetHostCleanup
    return
}

[IO.Directory]::CreateDirectory($workRoot) | Out-Null
Protect-DocMindJobDirectory -LiteralPath $workRoot
[IO.Directory]::CreateDirectory($receiptRoot) | Out-Null
Protect-DocMindJobDirectory -LiteralPath $receiptRoot
[IO.Directory]::CreateDirectory($previewWorkRoot) | Out-Null
Protect-DocMindJobDirectory -LiteralPath $previewWorkRoot
[IO.Directory]::CreateDirectory($previewReceiptRoot) | Out-Null
Protect-DocMindJobDirectory -LiteralPath $previewReceiptRoot
$removed = 0
$receiptReplay = [pscustomobject]@{ acknowledged = 0; pending = 0 }
if (-not $PreviewOnly) {
    $removed = Remove-DocMindExpiredJobDirectories -WorkRoot $workRoot -ReceiptRoot $receiptRoot -MinimumAgeSeconds $reaperMinimumAgeSeconds -ExcludedDirectoryNames @('previews')
    $receiptReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($receipt) Send-CleanupReceipt -Receipt $receipt }
}
$previewRemoved = Remove-DocMindExpiredJobDirectories -WorkRoot $previewWorkRoot -ReceiptRoot $previewReceiptRoot -MinimumAgeSeconds $reaperMinimumAgeSeconds
$previewReceiptReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $previewReceiptRoot -WorkRoot $previewWorkRoot -Sender { param($receipt) Send-CleanupReceipt -Receipt $receipt -Purpose preview }
$lastReceiptReplayAt = [DateTimeOffset]::UtcNow
Write-Output ("Host worker ready; stale job directories removed: {0}; preview directories removed: {1}; cleanup receipts acknowledged: {2}; preview receipts acknowledged: {3}." -f $removed, $previewRemoved, $receiptReplay.acknowledged, $previewReceiptReplay.acknowledged)

$runInitialScan = $true
if ($config.PSObject.Properties.Name -contains 'initial_scan_on_startup') { $runInitialScan = [bool]$config.initial_scan_on_startup }
if ($PreviewOnly) { $runInitialScan = $false }
if ($runInitialScan) {
    try {
        & (Join-Path $PSScriptRoot 'Invoke-DocMindSourceReconciliation.ps1') -ConfigPath $configFile -Reason startup
    } catch {
        $scanError = $_.Exception.Message
        if ($scanError -notmatch '^[A-Z0-9_]+$') { $scanError = 'SOURCE_RECONCILIATION_FAILED' }
        Write-Warning ("Startup reconciliation failed without deletion authority: {0}" -f $scanError)
    }
}

$consecutivePreviewClaims = 0
do {
    try {
        if ([DateTimeOffset]::UtcNow -ge $lastReceiptReplayAt.AddSeconds($cleanupReceiptReplaySeconds)) {
            if (-not $PreviewOnly) {
                $receiptReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($receipt) Send-CleanupReceipt -Receipt $receipt }
            }
            $previewReceiptReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $previewReceiptRoot -WorkRoot $previewWorkRoot -Sender { param($receipt) Send-CleanupReceipt -Receipt $receipt -Purpose preview }
            if ($receiptReplay.pending -gt 0) { Write-Warning ("Cleanup receipts awaiting a valid signed status ACK: {0}." -f $receiptReplay.pending) }
            if ($previewReceiptReplay.pending -gt 0) { Write-Warning ("Preview cleanup receipts awaiting a valid signed status ACK: {0}." -f $previewReceiptReplay.pending) }
            $lastReceiptReplayAt = [DateTimeOffset]::UtcNow
        }
        $claimBody = [ordered]@{ worker_id = [string]$config.worker_id; protocol_version = 1; lease_seconds = $claimLeaseSeconds }
        if ($allowedClaimFormats.Count -gt 0) { $claimBody.allowed_formats = @($allowedClaimFormats) }
        if ($SkipRetries) { $claimBody.skip_retries = $true }
        if (-not [string]::IsNullOrWhiteSpace($ClaimSourceId)) { $claimBody.claim_source_id = $ClaimSourceId }
        if (-not [string]::IsNullOrWhiteSpace($ClaimJobId)) { $claimBody.claim_job_id = $ClaimJobId; $claimBody.single_claim = $true }
        $claimOrder = if ($PreviewOnly) { @('preview') } elseif ($ClaimJobId) { @('ingest') } elseif (-not $previewEnabled) { @('ingest') } elseif ($consecutivePreviewClaims -ge 3) { @('ingest', 'preview') } else { @('preview', 'ingest') }
        foreach ($purpose in $claimOrder) {
            $path = if ($purpose -eq 'preview') { $previewClaimPath } else { $claimPath }
            try {
                $purposeClaimBody = Get-DocMindClaimRequestBody -Purpose $purpose -IngestionBody $claimBody
                $claimResult = Invoke-SignedJsonRequest -Method 'POST' -RelativeUri $path -Body $purposeClaimBody
            } catch {
                if ($purpose -ne 'preview') { throw }
                Write-Warning 'Preview claim unavailable; ingestion claim will continue.'
                continue
            }
            if ($claimResult.Body.Length -eq 0) { continue }
            $claimEnvelope = [Text.Encoding]::UTF8.GetString($claimResult.Body) | ConvertFrom-Json
            if ($null -ne $claimEnvelope.job) {
                $lease = if ($purpose -eq 'preview') { ConvertTo-PreviewLease -Claim $claimEnvelope.job } else { $claimEnvelope.job }
                $claimedJobId = [string]$lease.job_id
                $claimedFence = [Int64]$lease.fencing_token
                $claimKey = "${purpose}:$claimedJobId"
                if ($lastFencingTokenByJob.ContainsKey($claimKey) -and $claimedFence -le [Int64]$lastFencingTokenByJob[$claimKey]) { throw 'STALE_FENCING_TOKEN' }
                $lastFencingTokenByJob[$claimKey] = $claimedFence
                Invoke-LeasedDecryptJob -Lease $lease -Purpose $purpose
                if ($purpose -eq 'preview') { $consecutivePreviewClaims++ } else { $consecutivePreviewClaims = 0 }
                break
            }
        }
    } catch {
        $safeCode = $_.Exception.Message
        if ($safeCode -notmatch '^[A-Z0-9_]+$' -and -not $safeCode.StartsWith('API_HTTP_')) { $safeCode = 'HOST_WORKER_ERROR' }
        Write-Warning ("Host worker cycle failed: {0}" -f $safeCode)
    }
    if (-not $Once) { Start-Sleep -Seconds $PollSeconds }
} while (-not $Once)
