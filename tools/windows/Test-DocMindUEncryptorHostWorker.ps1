[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$testRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local/uEncryptor2/host-worker-self-test'))
if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
[IO.Directory]::CreateDirectory($testRoot) | Out-Null

function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Assert-Throws([scriptblock]$Action, [string]$Message) {
    try { & $Action; throw $Message } catch { if ($_.Exception.Message -eq $Message) { throw } }
}

try {
    $ingestionClaim = [ordered]@{ worker_id = 'worker-test'; protocol_version = 1; lease_seconds = 300; skip_retries = $true; claim_source_id = 'dept-2-e2e'; allowed_formats = @('hwp') }
    $previewClaim = Get-DocMindClaimRequestBody -Purpose preview -IngestionBody $ingestionClaim
    Assert-True (($previewClaim.Keys | Sort-Object) -join ',' -eq 'lease_seconds,protocol_version,worker_id') 'Preview claim leaked ingestion-only filters.'
    Assert-True ($previewClaim.worker_id -eq 'worker-test' -and $previewClaim.lease_seconds -eq 300) 'Preview claim identity or lease changed.'
    $unchangedClaim = Get-DocMindClaimRequestBody -Purpose ingest -IngestionBody $ingestionClaim
    Assert-True ($unchangedClaim.skip_retries -and $unchangedClaim.claim_source_id -eq 'dept-2-e2e') 'Preview changed ingestion claim policy.'
    $stateRoot = Join-Path $testRoot 'state-test'
    [IO.Directory]::CreateDirectory($stateRoot) | Out-Null
    Write-JobState -JobDirectory $stateRoot -Lease ([pscustomobject]@{
        job_id = 'job-test'; version_id = 'version-test'; fencing_token = 2
        lease_expires_at = '2026-09-22T01:00:00Z'
    }) -State 'DELIVERING'
    $savedState = Get-Content -LiteralPath (Join-Path $stateRoot 'job-state.json') -Raw | ConvertFrom-Json
    Assert-True ($savedState.job_id -eq 'job-test' -and $savedState.state -eq 'DELIVERING' -and $savedState.fencing_token -eq '2') 'Job state was not saved as a JSON object.'

    $startWorkerPath = Join-Path $PSScriptRoot 'Start-DocMindUEncryptorHostWorker.ps1'
    $parseTokens = $null
    $parseErrors = $null
    $startAst = [Management.Automation.Language.Parser]::ParseFile($startWorkerPath, [ref]$parseTokens, [ref]$parseErrors)
    Assert-True ($parseErrors.Count -eq 0) 'Host worker script did not parse.'
    $cleanupFunction = $startAst.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-TargetHostCleanup' }, $false)
    Assert-True ($null -ne $cleanupFunction) 'Target cleanup function is missing.'
    Invoke-Expression $cleanupFunction.Extent.Text
    $CleanupJobId = 'cleanup-test'
    $CleanupVersionId = 'cleanup-version'
    $CleanupFencingToken = 3L
    $CleanupPlaintextExtension = '.pdf'
    $AllowMalformedManifest = $false
    $workRoot = Join-Path $testRoot 'target-cleanup-work'
    $receiptRoot = Join-Path $testRoot 'target-cleanup-receipts'
    $jobsPath = '/api/v1/cloud-sync/host-worker/jobs'
    $config = [pscustomobject]@{ worker_id = 'host-test' }
    [IO.Directory]::CreateDirectory($workRoot) | Out-Null
    [IO.Directory]::CreateDirectory($receiptRoot) | Out-Null
    $targetDir = Join-Path $workRoot ($CleanupJobId + '-' + [Guid]::NewGuid().ToString('n'))
    [IO.Directory]::CreateDirectory($targetDir) | Out-Null
    [IO.File]::WriteAllText((Join-Path $targetDir 'job-state.json'), '"System.Collections.Specialized.OrderedDictionary"', [Text.Encoding]::UTF8)
    [IO.File]::WriteAllBytes((Join-Path $targetDir 'plaintext.pdf'), [byte[]](1, 2, 3, 4))
    $prepared = [ordered]@{
        job_id = $CleanupJobId; version_id = $CleanupVersionId; fencing_token = $CleanupFencingToken
        lease_expires_at = [DateTimeOffset]::UtcNow.AddMinutes(-1).ToString('o'); plaintext_size = 4
    }
    function Invoke-SignedJsonRequest { param($Method, $RelativeUri, $Body) return [pscustomobject]@{ Body = [Text.Encoding]::UTF8.GetBytes(($prepared | ConvertTo-Json -Compress)) } }
    $script:cleanupAcknowledged = $false
    function Send-CleanupReceipt { param($Receipt) $script:cleanupAcknowledged = ($Receipt.final_state -eq 'CLEANED') }
    Assert-Throws { Invoke-TargetHostCleanup } 'Malformed cleanup manifest was accepted without the explicit switch.'
    Assert-True (Test-Path -LiteralPath $targetDir) 'Rejected cleanup removed the target directory.'
    $AllowMalformedManifest = $true
    Invoke-TargetHostCleanup | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $targetDir)) 'Acknowledged target cleanup left a plaintext directory.'
    Assert-True $script:cleanupAcknowledged 'Target cleanup did not send the signed receipt.'
    Assert-True (@(Get-ChildItem -LiteralPath $receiptRoot -File).Count -eq 0) 'Acknowledged target cleanup left a receipt.'

    $key = [byte[]]::new(32)
    for ($index = 0; $index -lt $key.Length; $index++) { $key[$index] = [byte]($index + 1) }
    $body = [Text.Encoding]::UTF8.GetBytes('{"worker_id":"host-test","protocol_version":1}')
    $bodyHash = Get-DocMindBytesSha256 -Bytes $body
    Assert-True ($bodyHash -eq '7a60cc992e366dfb6d7dfd650cda49f9cb610b2815a2c3d9ecfafe04a821a90e') 'SHA-256 known-answer vector changed.'
    $knownSignature = Get-DocMindHmacSignature -Key $key -Method 'POST' -PathAndQuery '/api/v1/cloud-sync/host-worker/claim' -Timestamp '1700000000' -Nonce '00112233445566778899aabbccddeeff' -ContentSha256 $bodyHash
    Assert-True ($knownSignature -eq '0ba3c0e8dc6ccb33d54eb2621c6153cb3f5ee7ee780975f9767cfd2ed61e5c95') 'Cross-language HMAC known-answer vector failed.'
    $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $nonce = '00112233445566778899aabbccddeeff'
    $signature = Get-DocMindHmacSignature -Key $key -Method 'POST' -PathAndQuery '/api/v1/cloud-sync/host-worker/claim' -Timestamp $timestamp -Nonce $nonce -ContentSha256 $bodyHash
    Assert-True (Test-DocMindSignedMessage -Key $key -Method 'POST' -PathAndQuery '/api/v1/cloud-sync/host-worker/claim' -Timestamp $timestamp -Nonce $nonce -ContentSha256 $bodyHash -Signature $signature -BodyBytes $body) 'Valid signed message was rejected.'
    Assert-True (-not (Test-DocMindSignedMessage -Key $key -Method 'POST' -PathAndQuery '/api/v1/cloud-sync/host-worker/claim' -Timestamp $timestamp -Nonce $nonce -ContentSha256 $bodyHash -Signature ('0' * 64) -BodyBytes $body)) 'Tampered signature was accepted.'
    Assert-True ((New-DocMindRandomBytes -Count 16).Length -eq 16) 'Windows-compatible nonce generation failed.'

    $fixedNow = [DateTimeOffset]::Parse('2026-09-22T00:00:00Z')
    $artifactTimeout = Get-DocMindArtifactRequestTimeoutSeconds -LeaseExpiresAt $fixedNow.AddSeconds(300) -Now $fixedNow -SafetySeconds 5
    Assert-True ($artifactTimeout -eq 295) 'Artifact timeout was not bounded by signed lease remaining time.'
    Assert-True ($artifactTimeout -gt 120) 'Artifact ACK timeout was incorrectly tied to decrypt process timeout.'
    Assert-Throws { Get-DocMindArtifactRequestTimeoutSeconds -LeaseExpiresAt $fixedNow.AddSeconds(5) -Now $fixedNow -SafetySeconds 5 } 'Expired artifact lease was accepted.'
    $memoryStream = [IO.MemoryStream]::new([byte[]](1, 2, 3, 4))
    try {
        $streamContent = New-DocMindArtifactStreamContent -Stream $memoryStream -Length 4
        try { Assert-True ($streamContent.Headers.ContentLength -eq 4) 'Artifact Content-Length was not explicit.' } finally { $streamContent.Dispose() }
    } finally { $memoryStream.Dispose() }

    $sourceRoot = Join-Path $testRoot 'source'
    [IO.Directory]::CreateDirectory((Join-Path $sourceRoot 'folder')) | Out-Null
    $aclRoot = Join-Path $testRoot 'acl-job'
    [IO.Directory]::CreateDirectory($aclRoot) | Out-Null
    Protect-DocMindJobDirectory -LiteralPath $aclRoot
    Protect-DocMindJobDirectory -LiteralPath $aclRoot
    Assert-True (Get-Acl -LiteralPath $aclRoot).AreAccessRulesProtected 'Job ACL was not protected idempotently.'
    [IO.File]::WriteAllBytes((Join-Path $sourceRoot 'folder/sample.enc'), [byte[]](1, 2, 3, 4))
    $resolved = Resolve-DocMindSourceFile -Root $sourceRoot -RelativePath 'folder/sample.enc'
    Assert-True $resolved.EndsWith('sample.enc', [StringComparison]::OrdinalIgnoreCase) 'Safe relative path did not resolve.'
    Assert-Throws { Resolve-DocMindSourceFile -Root $sourceRoot -RelativePath '../outside.enc' } 'Traversal path was accepted.'
    Assert-Throws { Resolve-DocMindSourceFile -Root $sourceRoot -RelativePath 'C:\outside.enc' } 'Physical path was accepted.'

    $snapshot = @(Get-DocMindSourceSnapshotEntries -Root $sourceRoot)
    Assert-True ($snapshot.Count -eq 1) 'Full source snapshot did not enumerate exactly the synthetic file.'
    Assert-True ($snapshot[0].relative_path -eq 'folder/sample.enc') 'Snapshot exposed or changed the logical relative path.'
    Assert-True ($snapshot[0].ciphertext_sha256 -eq (Get-DocMindFileSha256 -LiteralPath $resolved)) 'Snapshot fingerprint changed.'
    Assert-True ($snapshot[0].host_file_id -match '^[0-9a-f]{8}:[0-9a-f]{16}:[0-9a-f]{16}$') 'Snapshot did not include a scoped NTFS identity.'
    $oldIdentity = $snapshot[0].host_file_id
    $movedPath = Join-Path $sourceRoot 'sample-moved.enc'
    Move-Item -LiteralPath $resolved -Destination $movedPath
    Assert-True ((Get-DocMindHostFileIdentity -LiteralPath $movedPath) -eq $oldIdentity) 'Same-volume move changed the file identity.'
    $copiedPath = Join-Path $sourceRoot 'sample-copy.enc'
    Copy-Item -LiteralPath $movedPath -Destination $copiedPath
    Assert-True ((Get-DocMindHostFileIdentity -LiteralPath $copiedPath) -ne $oldIdentity) 'Copy reused the source file identity.'
    Move-Item -LiteralPath $movedPath -Destination $resolved
    Remove-Item -LiteralPath $copiedPath
    Assert-True ($snapshot[0].PSObject.Properties.Name -notcontains 'root') 'Snapshot exposed the physical source root.'
    $unicodeName = ([string][char]0xD55C) + ([string][char]0xAE00) + '.doc'
    [IO.File]::WriteAllBytes((Join-Path $sourceRoot $unicodeName), [byte[]](5, 6, 7))
    $unicodeConfig = Join-Path $testRoot 'utf8-config.json'
    [IO.File]::WriteAllText($unicodeConfig, (@{ relative_path = $unicodeName } | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    $decodedRelativePath = [string](Get-Content -LiteralPath $unicodeConfig -Raw -Encoding UTF8 | ConvertFrom-Json).relative_path
    Assert-True ((Resolve-DocMindSourceFile -Root $sourceRoot -RelativePath $decodedRelativePath) -eq (Join-Path $sourceRoot $unicodeName)) 'UTF-8 source path was corrupted under Windows PowerShell.'
    Assert-True (-not (Test-DocMindConfirmedSourceFileAbsence -Root $sourceRoot -RelativePath 'folder/sample.enc')) 'Existing source file was reported deleted.'
    Assert-True (Test-DocMindConfirmedSourceFileAbsence -Root $sourceRoot -RelativePath 'folder/missing.enc') 'Accessible, absent source file was not confirmed deleted.'
    Assert-True (Test-DocMindConfirmedSourceFileAbsence -Root $sourceRoot -RelativePath 'removed-folder/missing.enc') 'Missing subtree under an accessible root was not confirmed deleted.'
    Assert-Throws { Test-DocMindConfirmedSourceFileAbsence -Root $sourceRoot -RelativePath '../outside.enc' } 'Traversal deletion proof was accepted.'
    $configuredSources = @(
        [pscustomobject]@{ source_id = 'approved-one' },
        [pscustomobject]@{ source_id = 'unapproved-many' }
    )
    Assert-True (@(Select-DocMindConfiguredSources -Sources $configuredSources -SourceId 'approved-one').Count -eq 1) 'Source-limited watcher selected more than one root.'
    Assert-True ((@(Select-DocMindConfiguredSources -Sources $configuredSources -SourceId 'approved-one')[0]).source_id -eq 'approved-one') 'Source-limited watcher selected the wrong root.'
    Assert-Throws { Select-DocMindConfiguredSources -Sources $configuredSources -SourceId 'missing' } 'Unknown source ID was accepted.'
    $readFailure = Get-DocMindSourceReadFailureCode -LiteralPath 'C:\\private\\document.doc' -ErrorRecord ([System.Management.Automation.ErrorRecord]::new([IO.IOException]::new('synthetic'), 'SYNTHETIC', [System.Management.Automation.ErrorCategory]::ReadError, $null))
    Assert-True ($readFailure -match '^SOURCE_FILE_READ_FAILED_DOC_[0-9A-F]{8}$') 'Source scan failure code did not classify a DOC without its path.'
    Assert-True (-not $readFailure.Contains('private')) 'Source scan failure code leaked the physical path.'
    $retryStarted = [DateTimeOffset]::Parse('2026-09-28T00:00:00Z')
    $retryAllowedAt = $retryStarted.AddSeconds(300)
    Assert-True (-not (Test-DocMindDiscoveryScanDue -Now $retryStarted.AddSeconds(5) -EventDue $true -FallbackDue $true -FailureNotBefore $retryAllowedAt)) 'Discovery event bypassed failure backoff.'
    Assert-True (Test-DocMindDiscoveryScanDue -Now $retryAllowedAt -EventDue $true -FallbackDue $false -FailureNotBefore $retryAllowedAt) 'Discovery did not resume at failure backoff deadline.'
    Assert-True (Test-DocMindDiscoveryScanDue -Now $retryStarted -EventDue $false -FallbackDue $true) 'Healthy discovery fallback was blocked.'

    $scanScript = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Invoke-DocMindSourceReconciliation.ps1') -Raw
    Assert-True $scanScript.Contains("'/api/v1/cloud-sync/host-worker/scans/events'") 'Full-scan endpoint contract is missing.'
    Assert-True $scanScript.Contains("'/api/v1/cloud-sync/host-worker/reconciliation/claim'") 'Signed midnight-scan claim endpoint is missing.'
    foreach ($field in @('root_access_confirmed', 'complete', 'file_count', 'batch_count', 'batch_index')) {
        Assert-True $scanScript.Contains($field) ("Full-scan contract is missing {0}." -f $field)
    }
    Assert-True $scanScript.Contains('$failed.complete = $false') 'Failed scans do not explicitly revoke deletion authority.'
    Assert-True $scanScript.Contains("'^midnight-\d{4}-\d{2}-\d{2}$'") 'Scheduled scan IDs are not constrained to the signed Asia/Seoul date.'
    Assert-True $scanScript.Contains('$started.schedule_fencing_token = $ScheduleFencingToken') 'Scheduled scan fencing is not echoed in the started event.'
    $watchScript = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Watch-DocMindEncryptedSources.ps1') -Raw
    Assert-True $watchScript.Contains("'/api/v1/cloud-sync/host-worker/deletions'") 'Deletion endpoint contract is missing.'
    Assert-True $watchScript.Contains('Test-DocMindConfirmedSourceFileAbsence') 'Deletion events are not guarded by an absence proof.'
    Assert-True $watchScript.Contains('Get-Content -LiteralPath $configFile -Raw -Encoding UTF8') 'Watch config must decode UTF-8 source paths on Windows PowerShell.'
    Assert-True $watchScript.Contains('FileSystemWatcher') 'Discovery event watcher is missing.'
    Assert-True $watchScript.Contains('DiscoveryFallbackSeconds') 'Discovery fallback scan is missing.'
    Assert-True $watchScript.Contains('DiscoveryFailureRetrySeconds') 'Discovery failure backoff is missing.'
    $planConfig = Join-Path $testRoot 'schedule-config.json'
    [IO.File]::WriteAllText($planConfig, (@{
        daily_reconciliation_timezone = 'Asia/Seoul'
        daily_reconciliation_local_time = '00:00'
    } | ConvertTo-Json))
    if ([TimeZoneInfo]::Local.Id -eq 'Korea Standard Time') {
        $taskPlan = & (Join-Path $PSScriptRoot 'Get-DocMindReconciliationTaskPlan.ps1') -ConfigPath $planConfig
        Assert-True ($taskPlan.trigger -eq 'Daily at 00:00 local time') 'Daily reconciliation trigger changed from midnight.'
        Assert-True ($taskPlan.required_windows_time_zone -eq 'Korea Standard Time') 'Asia/Seoul Windows time-zone guard is missing.'
        Assert-True ($taskPlan.registration_performed -eq $false) 'Task-plan generator unexpectedly registered an operating-system task.'
        Assert-True $taskPlan.arguments.Contains('-Reason scheduled') 'Scheduled reconciliation action is incomplete.'
        Assert-True ($taskPlan.repeat_every_minutes -eq 15) 'Scheduled reconciliation repetition is missing.'
        Assert-True ($taskPlan.restart_on_failure_interval_minutes -eq 15) 'Scheduled retry interval is missing.'
    } else {
        Assert-Throws { & (Join-Path $PSScriptRoot 'Get-DocMindReconciliationTaskPlan.ps1') -ConfigPath $planConfig } 'Non-Korea Windows time zone was accepted for a local-midnight task.'
    }

    $lease = [pscustomobject]@{
        job_id = 'job-1'; source_id = 'source-1'; document_id = 'document-1'; version_id = 'version-1'
        relative_path = 'folder/sample.enc'; ciphertext_sha256 = ('a' * 64); fencing_token = 7
        lease_expires_at = [DateTimeOffset]::UtcNow.AddMinutes(5).ToString('o')
    }
    $expiry = Assert-DocMindLeasePayload -Payload $lease
    Assert-True ($expiry -gt [DateTimeOffset]::UtcNow) 'Valid lease was rejected.'
    $jsonLease = ($lease | ConvertTo-Json -Compress) | ConvertFrom-Json
    $jsonExpiry = Assert-DocMindLeasePayload -Payload $jsonLease
    Assert-True ($jsonExpiry -gt [DateTimeOffset]::UtcNow) 'ConvertFrom-Json changed the signed lease timezone.'
    $lease.relative_path = 'D:\secret.enc'
    Assert-Throws { Assert-DocMindLeasePayload -Payload $lease } 'Physical source path in lease was accepted.'

    $workRoot = Join-Path $testRoot 'jobs'
    $receiptRoot = Join-Path $testRoot 'cleanup-receipts'
    $expired = Join-Path $workRoot 'expired-job'
    $active = Join-Path $workRoot 'active-job'
    $previewContainer = Join-Path $workRoot 'previews'
    [IO.Directory]::CreateDirectory($expired) | Out-Null
    [IO.Directory]::CreateDirectory($active) | Out-Null
    [IO.Directory]::CreateDirectory($previewContainer) | Out-Null
    [IO.File]::WriteAllText((Join-Path $expired 'plaintext.bin'), 'synthetic')
    [IO.File]::WriteAllText((Join-Path $active 'plaintext.bin'), 'synthetic')
    [IO.File]::WriteAllText((Join-Path $expired 'job-state.json'), (@{ job_id = 'expired-job'; version_id = 'version-expired'; fencing_token = 3; lease_expires_at = [DateTimeOffset]::UtcNow.AddMinutes(-10).ToString('o') } | ConvertTo-Json))
    [IO.File]::WriteAllText((Join-Path $active 'job-state.json'), (@{ job_id = 'active-job'; version_id = 'version-active'; fencing_token = 4; lease_expires_at = [DateTimeOffset]::UtcNow.AddMinutes(10).ToString('o') } | ConvertTo-Json))
    (Get-Item -LiteralPath $expired).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
    (Get-Item -LiteralPath $active).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
    (Get-Item -LiteralPath $previewContainer).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
    $removed = Remove-DocMindExpiredJobDirectories -WorkRoot $workRoot -ReceiptRoot $receiptRoot -MinimumAgeSeconds 30 -ExcludedDirectoryNames @('previews')
    Assert-True ($removed -eq 1) 'Reaper did not remove exactly one expired job.'
    Assert-True (-not (Test-Path -LiteralPath $expired)) 'Expired job survived reaping.'
    Assert-True (Test-Path -LiteralPath $active) 'Active leased job was removed.'
    Assert-True (Test-Path -LiteralPath $previewContainer) 'Ingestion reaper removed the preview work container.'
    $receipts = @(Get-ChildItem -LiteralPath $receiptRoot -File -Filter '*.json')
    Assert-True ($receipts.Count -eq 1) 'Reaper did not persist exactly one cleanup receipt.'
    $receipt = Read-DocMindCleanupReceipt -LiteralPath $receipts[0].FullName -ReceiptRoot $receiptRoot
    Assert-True ([string]$receipt.job_id -eq 'expired-job') 'Cleanup receipt job identity changed.'
    Assert-True ([string]$receipt.version_id -eq 'version-expired') 'Cleanup receipt version identity changed.'
    Assert-True ([Int64]$receipt.fencing_token -eq 3) 'Cleanup receipt fence changed.'
    Assert-True ([string]$receipt.final_state -eq 'CLEANED') 'Reaper reported cleanup before deletion completed.'
    $receiptFields = @($receipt.PSObject.Properties.Name)
    Assert-True ($receiptFields.Count -eq 5) 'Cleanup receipt contains unexpected fields.'
    foreach ($forbidden in @('root', 'path', 'content', 'hash', 'relative_path', 'ciphertext_sha256')) {
        Assert-True ($receiptFields -notcontains $forbidden) ("Cleanup receipt leaked forbidden field {0}." -f $forbidden)
    }
    $failedReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($item) throw 'SYNTHETIC_STATUS_ACK_FAILURE' }
    Assert-True ($failedReplay.acknowledged -eq 0 -and $failedReplay.pending -eq 1) 'Failed status replay removed its durable receipt.'
    $script:capturedCleanupReceipt = $null
    $successfulReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($item) $script:capturedCleanupReceipt = $item }
    Assert-True ($successfulReplay.acknowledged -eq 1 -and $successfulReplay.pending -eq 0) 'Successful status replay did not acknowledge one receipt.'
    Assert-True ([Int64]$script:capturedCleanupReceipt.fencing_token -eq 3) 'Status replay changed the fencing token.'
    Assert-True (@(Get-ChildItem -LiteralPath $receiptRoot -File -Filter '*.json').Count -eq 0) 'Acknowledged cleanup receipt was not deleted.'
    $pendingDirectory = Join-Path $workRoot 'pending-job-synthetic'
    [IO.Directory]::CreateDirectory($pendingDirectory) | Out-Null
    [void](Write-DocMindCleanupReceiptAtomic -ReceiptRoot $receiptRoot -JobId 'pending-job' -VersionId 'version-pending' -FencingToken 5 -FinalState 'CLEANUP_PENDING' -ErrorCode $null)
    $script:pendingReceiptWasSent = $false
    $pendingReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($item) $script:pendingReceiptWasSent = $true }
    Assert-True ($pendingReplay.pending -eq 1 -and -not $script:pendingReceiptWasSent) 'CLEANED was reported before the plaintext directory was deleted.'
    Remove-Item -LiteralPath $pendingDirectory -Force
    $promotedReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($item) $script:pendingReceiptWasSent = ([string]$item.final_state -eq 'CLEANED') }
    Assert-True ($promotedReplay.acknowledged -eq 1 -and $script:pendingReceiptWasSent) 'Deleted plaintext did not promote its pending receipt to CLEANED.'

    $previewReceiptRoot = Join-Path $testRoot 'preview-cleanup-receipts'
    [void](Write-DocMindCleanupReceiptAtomic -ReceiptRoot $receiptRoot -JobId 'same-job' -VersionId 'same-version' -FencingToken 6 -FinalState 'CLEANED' -ErrorCode $null)
    [void](Write-DocMindCleanupReceiptAtomic -ReceiptRoot $previewReceiptRoot -JobId 'same-job' -VersionId 'same-version' -FencingToken 6 -FinalState 'CLEANED' -ErrorCode $null)
    $ingestReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $receiptRoot -WorkRoot $workRoot -Sender { param($item) }
    Assert-True ($ingestReplay.acknowledged -eq 1) 'Ingestion cleanup receipt was not replayed.'
    Assert-True (@(Get-ChildItem -LiteralPath $previewReceiptRoot -File -Filter '*.json').Count -eq 1) 'Ingestion replay consumed a preview receipt with the same job identity.'
    $previewReplay = Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $previewReceiptRoot -Sender { param($item) }
    Assert-True ($previewReplay.acknowledged -eq 1) 'Preview cleanup receipt was not replayed separately.'
    $workerScript = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Start-DocMindUEncryptorHostWorker.ps1') -Raw
    Assert-True $workerScript.Contains("'/api/v1/cloud-sync/host-worker/preview/claim'") 'Preview claim endpoint contract is missing.'
    Assert-True $workerScript.Contains('$consecutivePreviewClaims -ge 3') 'Preview fairness bound is missing.'
    Assert-True $workerScript.Contains('preview_enabled') 'Preview opt-in switch is missing.'
    Assert-True $workerScript.Contains('if ($PreviewOnly -and -not $previewEnabled)') 'Preview-only smoke must refuse a disabled preview config.'
    Assert-True $workerScript.Contains('if ($PreviewOnly) { @(''preview'') }') 'Preview-only smoke must not claim ingestion.'
    Assert-True $workerScript.Contains('if ($PreviewOnly) { $runInitialScan = $false }') 'Preview-only smoke must skip source reconciliation.'

    Write-Output 'Host worker self-test passed: HMAC/tamper, lease-bounded ACK timeout, explicit Content-Length, ACL idempotency, path boundary, full-scan/deletion proof, fencing, reaping, and purpose-isolated durable cleanup-receipt replay.'
} finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
