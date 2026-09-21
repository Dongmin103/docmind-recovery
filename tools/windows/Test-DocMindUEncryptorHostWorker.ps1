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

    $lease = [pscustomobject]@{
        job_id = 'job-1'; source_id = 'source-1'; document_id = 'document-1'; version_id = 'version-1'
        relative_path = 'folder/sample.enc'; ciphertext_sha256 = ('a' * 64); fencing_token = 7
        lease_expires_at = [DateTimeOffset]::UtcNow.AddMinutes(5).ToString('o')
    }
    $expiry = Assert-DocMindLeasePayload -Payload $lease
    Assert-True ($expiry -gt [DateTimeOffset]::UtcNow) 'Valid lease was rejected.'
    $lease.relative_path = 'D:\secret.enc'
    Assert-Throws { Assert-DocMindLeasePayload -Payload $lease } 'Physical source path in lease was accepted.'

    $workRoot = Join-Path $testRoot 'jobs'
    $expired = Join-Path $workRoot 'expired-job'
    $active = Join-Path $workRoot 'active-job'
    [IO.Directory]::CreateDirectory($expired) | Out-Null
    [IO.Directory]::CreateDirectory($active) | Out-Null
    [IO.File]::WriteAllText((Join-Path $expired 'plaintext.bin'), 'synthetic')
    [IO.File]::WriteAllText((Join-Path $active 'plaintext.bin'), 'synthetic')
    [IO.File]::WriteAllText((Join-Path $expired 'job-state.json'), (@{ lease_expires_at = [DateTimeOffset]::UtcNow.AddMinutes(-10).ToString('o') } | ConvertTo-Json))
    [IO.File]::WriteAllText((Join-Path $active 'job-state.json'), (@{ lease_expires_at = [DateTimeOffset]::UtcNow.AddMinutes(10).ToString('o') } | ConvertTo-Json))
    (Get-Item -LiteralPath $expired).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
    (Get-Item -LiteralPath $active).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
    $removed = Remove-DocMindExpiredJobDirectories -WorkRoot $workRoot -MinimumAgeSeconds 30
    Assert-True ($removed -eq 1) 'Reaper did not remove exactly one expired job.'
    Assert-True (-not (Test-Path -LiteralPath $expired)) 'Expired job survived reaping.'
    Assert-True (Test-Path -LiteralPath $active) 'Active leased job was removed.'

    Write-Output 'Host worker self-test passed: HMAC/tamper, lease-bounded ACK timeout, explicit Content-Length, ACL idempotency, path boundary, fencing, and reaping.'
} finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
