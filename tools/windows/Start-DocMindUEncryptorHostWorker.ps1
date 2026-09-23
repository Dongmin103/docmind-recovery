[CmdletBinding()]
param(
    [string]$ConfigPath = $env:DOCMIND_HOST_WORKER_CONFIG,
    [switch]$Once,
    [ValidateRange(1, 300)][int]$PollSeconds = 5
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')

if ([string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'DOCMIND_HOST_WORKER_CONFIG or -ConfigPath is required.' }
$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw 'Host worker config file is missing.' }
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json

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
$secretFile = [IO.Path]::GetFullPath([string]$config.shared_secret_file)
if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or [IO.Path]::GetExtension($executable) -ne '.exe') { throw 'Configured executable is unavailable or is not an .exe.' }
if ($executable.Contains('"') -or $workRoot.Contains('"') -or $receiptRoot.Contains('"')) { throw 'Configured paths containing quotation marks are unsupported.' }

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
    param($Lease, [string]$State, [string]$ErrorCode, [bool]$CleanupComplete)
    $status = if ($State -eq 'COMPLETE' -and $CleanupComplete) { 'CLEANED' } elseif (-not $CleanupComplete) { 'CLEANUP_FAILED' } else { 'FAILED' }
    $body = [ordered]@{
        worker_id = [string]$config.worker_id
        version_id = [string]$Lease.version_id
        fencing_token = [Int64]$Lease.fencing_token
        status = $status
        error_code = $ErrorCode
    }
    Send-WorkerStatusBody -JobId ([string]$Lease.job_id) -Body $body
}

function Send-WorkerStatusBody {
    param([string]$JobId, $Body)
    $result = Invoke-SignedJsonRequest -Method 'POST' -RelativeUri ("$jobsPath/$([Uri]::EscapeDataString($JobId))/status") -Body $Body
    if ($result.Body.Length -eq 0) { throw 'STATUS_ACK_MISSING' }
    $ack = [Text.Encoding]::UTF8.GetString($result.Body) | ConvertFrom-Json
    if ($ack.accepted -ne $true -or [string]$ack.job_id -ne $JobId -or [string]$ack.version_id -ne [string]$Body.version_id -or [Int64]$ack.fencing_token -ne [Int64]$Body.fencing_token) { throw 'STATUS_ACK_MISMATCH' }
}

function Send-CleanupReceipt {
    param($Receipt)
    $body = [ordered]@{
        worker_id = [string]$config.worker_id
        version_id = [string]$Receipt.version_id
        fencing_token = [Int64]$Receipt.fencing_token
        status = [string]$Receipt.final_state
        error_code = $Receipt.error_code
    }
    Send-WorkerStatusBody -JobId ([string]$Receipt.job_id) -Body $body
}

function Write-JobState {
    param([string]$JobDirectory, $Lease, [string]$State)
    $state = [ordered]@{
        schema_version = 1
        job_id = [string]$Lease.job_id
        version_id = [string]$Lease.version_id
        fencing_token = [string]$Lease.fencing_token
        lease_expires_at = (ConvertTo-DocMindDateTimeOffset -Value $Lease.lease_expires_at -Name 'lease_expires_at').ToString('o')
        state = $State
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
    }
    [IO.File]::WriteAllText((Join-Path $JobDirectory 'job-state.json'), ($state | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
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
    param($Lease)
    $leaseExpiry = Assert-DocMindLeasePayload -Payload $Lease -MinimumRemainingSeconds $leaseSafetySeconds
    if (-not $sources.ContainsKey([string]$Lease.source_id)) { throw 'SOURCE_NOT_REGISTERED' }
    $sourceFile = Resolve-DocMindSourceFile -Root $sources[[string]$Lease.source_id] -RelativePath ([string]$Lease.relative_path)
    $sourceHashBefore = Get-DocMindFileSha256 -LiteralPath $sourceFile
    if (-not (Test-DocMindFixedTimeHexEqual $sourceHashBefore ([string]$Lease.ciphertext_sha256).ToLowerInvariant())) { throw 'SOURCE_FINGERPRINT_MISMATCH' }
    $executableHashBefore = Assert-ExecutableIdentity

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

        $sourceHashAfter = Get-DocMindFileSha256 -LiteralPath $sourceFile
        $executableHashAfter = Assert-ExecutableIdentity
        if (-not (Test-DocMindFixedTimeHexEqual $sourceHashBefore $sourceHashAfter) -or -not (Test-DocMindFixedTimeHexEqual $executableHashBefore $executableHashAfter)) { throw 'INPUT_FINGERPRINT_CHANGED' }
        if ([DateTimeOffset]::UtcNow.AddSeconds($leaseSafetySeconds) -ge $leaseExpiry) { throw 'LEASE_EXPIRED_BEFORE_DELIVERY' }

        Write-JobState -JobDirectory $jobDirectory -Lease $Lease -State 'DELIVERING'
        $plainHash = Get-DocMindFileSha256 -LiteralPath $outputFile
        $stream = [IO.File]::Open($outputFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        try {
            $content = New-DocMindArtifactStreamContent -Stream $stream -Length $outputItem.Length
            $relativeUri = "$jobsPath/$([Uri]::EscapeDataString([string]$Lease.job_id))/artifact"
            $artifactHeaders = @{
                'X-DocMind-Worker-Id' = [string]$config.worker_id
                'X-DocMind-Version-Id' = [string]$Lease.version_id
                'X-DocMind-Fencing-Token' = [string][Int64]$Lease.fencing_token
                'X-DocMind-Plaintext-SHA256' = $plainHash
                'X-DocMind-Plaintext-Size' = [string]$outputItem.Length
            }
            $artifactTimeoutSeconds = Get-DocMindArtifactRequestTimeoutSeconds -LeaseExpiresAt $leaseExpiry -SafetySeconds 5
            $result = Invoke-SignedBytesRequest -Method 'PUT' -RelativeUri $relativeUri -ContentSha256 $plainHash -Content $content -ContentType 'application/octet-stream' -AdditionalHeaders $artifactHeaders -RequestTimeoutSeconds $artifactTimeoutSeconds
        } finally { $stream.Dispose() }
        if ($result.Body.Length -eq 0) { throw 'DELIVERY_ACK_MISSING' }
        $ack = [Text.Encoding]::UTF8.GetString($result.Body) | ConvertFrom-Json
        if ($ack.accepted -ne $true -or [string]$ack.job_id -ne [string]$Lease.job_id -or [string]$ack.version_id -ne [string]$Lease.version_id -or [Int64]$ack.fencing_token -ne [Int64]$Lease.fencing_token) { throw 'DELIVERY_ACK_MISMATCH' }
        $jobSucceeded = $true
        Write-JobState -JobDirectory $jobDirectory -Lease $Lease -State 'ACKNOWLEDGED'
    } catch {
        $errorCode = $_.Exception.Message
        if ($errorCode -notmatch '^[A-Z0-9_]+$') { $errorCode = 'HOST_WORKER_ERROR' }
    } finally {
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
    }
    $finalState = if ($jobSucceeded -and $cleanupComplete) { 'COMPLETE' } elseif (-not $cleanupComplete) { 'CLEANUP_FAILED' } else { 'FAILED' }
    if ($cleanupComplete) {
        $receiptFinalState = if ($finalState -eq 'COMPLETE') { 'CLEANED' } else { 'FAILED' }
        try {
            $receiptPath = Write-DocMindCleanupReceiptAtomic -ReceiptRoot $receiptRoot -JobId ([string]$Lease.job_id) -VersionId ([string]$Lease.version_id) -FencingToken ([Int64]$Lease.fencing_token) -FinalState $receiptFinalState -ErrorCode $errorCode
        } catch { $jobSucceeded = $false; $errorCode = 'CLEANUP_RECEIPT_WRITE_FAILED' }
    }
    try {
        Send-WorkerStatus -Lease $Lease -State $finalState -ErrorCode $errorCode -CleanupComplete $cleanupComplete
        if ($cleanupComplete -and $null -ne $receiptPath -and (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
            Assert-DocMindNoReparsePoint -LiteralPath $receiptPath -Boundary $receiptRoot -Name 'Acknowledged cleanup receipt'
            Remove-Item -LiteralPath $receiptPath -Force -ErrorAction Stop
        }
    } catch { if ($jobSucceeded) { $jobSucceeded = $false; $errorCode = 'STATUS_ACK_FAILED' } }
    if (-not $jobSucceeded) { throw $errorCode }
}

[void](Assert-ExecutableIdentity)
[IO.Directory]::CreateDirectory($workRoot) | Out-Null
Protect-DocMindJobDirectory -LiteralPath $workRoot
[IO.Directory]::CreateDirectory($receiptRoot) | Out-Null
Protect-DocMindJobDirectory -LiteralPath $receiptRoot
$removed = Remove-DocMindExpiredJobDirectories -WorkRoot $workRoot -ReceiptRoot $receiptRoot -MinimumAgeSeconds $reaperMinimumAgeSeconds
$receiptReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($receipt) Send-CleanupReceipt -Receipt $receipt }
$lastReceiptReplayAt = [DateTimeOffset]::UtcNow
Write-Output ("Host worker ready; stale job directories removed: {0}; cleanup receipts acknowledged: {1}; pending: {2}." -f $removed, $receiptReplay.acknowledged, $receiptReplay.pending)

$runInitialScan = $true
if ($config.PSObject.Properties.Name -contains 'initial_scan_on_startup') { $runInitialScan = [bool]$config.initial_scan_on_startup }
if ($runInitialScan) {
    try {
        & (Join-Path $PSScriptRoot 'Invoke-DocMindSourceReconciliation.ps1') -ConfigPath $configFile -Reason startup
    } catch {
        $scanError = $_.Exception.Message
        if ($scanError -notmatch '^[A-Z0-9_]+$') { $scanError = 'SOURCE_RECONCILIATION_FAILED' }
        Write-Warning ("Startup reconciliation failed without deletion authority: {0}" -f $scanError)
    }
}

do {
    try {
        if ([DateTimeOffset]::UtcNow -ge $lastReceiptReplayAt.AddSeconds($cleanupReceiptReplaySeconds)) {
            $receiptReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($receipt) Send-CleanupReceipt -Receipt $receipt }
            if ($receiptReplay.pending -gt 0) { Write-Warning ("Cleanup receipts awaiting a valid signed status ACK: {0}." -f $receiptReplay.pending) }
            $lastReceiptReplayAt = [DateTimeOffset]::UtcNow
        }
        $claimBody = [ordered]@{ worker_id = [string]$config.worker_id; protocol_version = 1; lease_seconds = $claimLeaseSeconds }
        $claimResult = Invoke-SignedJsonRequest -Method 'POST' -RelativeUri $claimPath -Body $claimBody
        if ($claimResult.Body.Length -gt 0) {
            $claimEnvelope = [Text.Encoding]::UTF8.GetString($claimResult.Body) | ConvertFrom-Json
            if ($null -ne $claimEnvelope.job) {
                $claimedJobId = [string]$claimEnvelope.job.job_id
                $claimedFence = [Int64]$claimEnvelope.job.fencing_token
                if ($lastFencingTokenByJob.ContainsKey($claimedJobId) -and $claimedFence -le [Int64]$lastFencingTokenByJob[$claimedJobId]) { throw 'STALE_FENCING_TOKEN' }
                $lastFencingTokenByJob[$claimedJobId] = $claimedFence
                Invoke-LeasedDecryptJob -Lease $claimEnvelope.job
            }
        }
    } catch {
        $safeCode = $_.Exception.Message
        if ($safeCode -notmatch '^[A-Z0-9_]+$' -and -not $safeCode.StartsWith('API_HTTP_')) { $safeCode = 'HOST_WORKER_ERROR' }
        Write-Warning ("Host worker cycle failed: {0}" -f $safeCode)
    }
    if (-not $Once) { Start-Sleep -Seconds $PollSeconds }
} while (-not $Once)
