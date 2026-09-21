Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

function ConvertTo-DocMindHex {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    return ([BitConverter]::ToString($Bytes) -replace '-', '').ToLowerInvariant()
}

function Get-DocMindBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ConvertTo-DocMindHex -Bytes ($sha.ComputeHash($Bytes)) } finally { $sha.Dispose() }
}

function Get-DocMindFileSha256 {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    return (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-DocMindHmacSignature {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Key,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$PathAndQuery,
        [Parameter(Mandatory = $true)][string]$Timestamp,
        [Parameter(Mandatory = $true)][string]$Nonce,
        [Parameter(Mandatory = $true)][string]$ContentSha256
    )
    $canonical = "{0}`n{1}`n{2}`n{3}`n{4}" -f $Method.ToUpperInvariant(), $PathAndQuery, $Timestamp, $Nonce, $ContentSha256.ToLowerInvariant()
    $hmac = [Security.Cryptography.HMACSHA256]::new($Key)
    try { return ConvertTo-DocMindHex -Bytes ($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))) } finally { $hmac.Dispose() }
}

function Test-DocMindFixedTimeHexEqual {
    param([string]$Left, [string]$Right)
    if ($null -eq $Left -or $null -eq $Right -or $Left.Length -ne $Right.Length) { return $false }
    $different = 0
    for ($index = 0; $index -lt $Left.Length; $index++) {
        $different = $different -bor ([int][char]$Left[$index] -bxor [int][char]$Right[$index])
    }
    return $different -eq 0
}

function Read-DocMindSharedSecret {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) { throw 'Shared secret file is missing.' }
    $raw = [IO.File]::ReadAllBytes([IO.Path]::GetFullPath($LiteralPath))
    if ($raw.Length -eq 32) { return $raw }
    $value = [Text.Encoding]::UTF8.GetString($raw).Trim()
    try {
        if ($value -match '^[a-fA-F0-9]{64}$') {
            $key = [byte[]]::new(32)
            for ($index = 0; $index -lt 32; $index++) { $key[$index] = [Convert]::ToByte($value.Substring($index * 2, 2), 16) }
        } else {
            $key = [Convert]::FromBase64String($value)
        }
    } catch { throw 'Shared secret must be exactly 32 bytes encoded as hex or Base64.' }
    if ($key.Length -ne 32) { throw 'Shared secret must decode to exactly 32 bytes.' }
    return $key
}

function Assert-DocMindIdentifier {
    param([Parameter(Mandatory = $true)][string]$Value, [Parameter(Mandatory = $true)][string]$Name)
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$') { throw "$Name has an invalid format." }
}

function Assert-DocMindNoReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Boundary,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $path = [IO.Path]::GetFullPath($LiteralPath)
    $root = [IO.Path]::GetFullPath($Boundary).TrimEnd('\', '/')
    $prefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $path.Equals($root, [StringComparison]::OrdinalIgnoreCase) -and -not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name escaped its trusted boundary."
    }
    $current = $path
    while ($current.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Name must not traverse a reparse point." }
        }
        if ($current.Equals($root, [StringComparison]::OrdinalIgnoreCase)) { break }
        $current = Split-Path -Parent $current
    }
}

function Resolve-DocMindSourceFile {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    if ([IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Contains(':') -or $RelativePath.Contains([char]0)) { throw 'relative_path must be a relative filesystem path.' }
    $segments = $RelativePath -split '[\\/]'
    if ($segments.Count -eq 0 -or @($segments | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) { throw 'relative_path contains an unsafe segment.' }
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $rootPath -PathType Container)) { throw 'Configured source root is unavailable.' }
    $candidate = [IO.Path]::GetFullPath((Join-Path $rootPath ($segments -join [IO.Path]::DirectorySeparatorChar)))
    Assert-DocMindNoReparsePoint -LiteralPath $candidate -Boundary $rootPath -Name 'Source file'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw 'Encrypted source file is unavailable.' }
    return $candidate
}

function Assert-DocMindLeasePayload {
    param([Parameter(Mandatory = $true)]$Payload, [int]$MinimumRemainingSeconds = 5)
    $required = @('job_id', 'source_id', 'document_id', 'version_id', 'relative_path', 'ciphertext_sha256', 'fencing_token', 'lease_expires_at')
    $allowed = $required
    $actual = @($Payload.PSObject.Properties.Name)
    foreach ($name in $required) { if ($actual -notcontains $name -or [string]::IsNullOrWhiteSpace([string]$Payload.$name)) { throw "Signed lease is missing $name." } }
    foreach ($name in $actual) { if ($allowed -notcontains $name) { throw "Signed lease contains unsupported field $name." } }
    foreach ($name in @('job_id', 'source_id', 'document_id', 'version_id')) { Assert-DocMindIdentifier -Value ([string]$Payload.$name) -Name $name }
    $fencingToken = 0L
    if (-not [Int64]::TryParse([string]$Payload.fencing_token, [ref]$fencingToken) -or $fencingToken -lt 1) { throw 'fencing_token is invalid.' }
    if ([string]$Payload.ciphertext_sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'ciphertext_sha256 is invalid.' }
    $expires = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse([string]$Payload.lease_expires_at, [ref]$expires)) { throw 'lease_expires_at is invalid.' }
    if ($expires -le [DateTimeOffset]::UtcNow.AddSeconds($MinimumRemainingSeconds)) { throw 'Signed lease is expired or too close to expiry.' }
    if ([IO.Path]::IsPathRooted([string]$Payload.relative_path) -or ([string]$Payload.relative_path).Contains(':')) { throw 'Signed lease contains a physical path.' }
    return $expires.ToUniversalTime()
}

function Test-DocMindSignedMessage {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Key,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$PathAndQuery,
        [Parameter(Mandatory = $true)][string]$Timestamp,
        [Parameter(Mandatory = $true)][string]$Nonce,
        [Parameter(Mandatory = $true)][string]$ContentSha256,
        [Parameter(Mandatory = $true)][string]$Signature,
        [Parameter(Mandatory = $true)][byte[]]$BodyBytes,
        [int]$MaximumClockSkewSeconds = 60
    )
    if ($Timestamp -notmatch '^\d{10}$' -or $Nonce -notmatch '^[a-fA-F0-9]{32}$' -or $ContentSha256 -notmatch '^[a-fA-F0-9]{64}$' -or $Signature -notmatch '^[a-fA-F0-9]{64}$') { return $false }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ([Math]::Abs($now - [Int64]$Timestamp) -gt $MaximumClockSkewSeconds) { return $false }
    $actualHash = Get-DocMindBytesSha256 -Bytes $BodyBytes
    if (-not (Test-DocMindFixedTimeHexEqual $actualHash $ContentSha256.ToLowerInvariant())) { return $false }
    $expected = Get-DocMindHmacSignature -Key $Key -Method $Method -PathAndQuery $PathAndQuery -Timestamp $Timestamp -Nonce $Nonce -ContentSha256 $ContentSha256
    return Test-DocMindFixedTimeHexEqual $expected $Signature.ToLowerInvariant()
}

function Get-DocMindArtifactRequestTimeoutSeconds {
    param(
        [Parameter(Mandatory = $true)][DateTimeOffset]$LeaseExpiresAt,
        [DateTimeOffset]$Now = [DateTimeOffset]::UtcNow,
        [ValidateRange(1, 60)][int]$SafetySeconds = 5
    )
    $seconds = [Math]::Floor(($LeaseExpiresAt.ToUniversalTime() - $Now.ToUniversalTime()).TotalSeconds) - $SafetySeconds
    if ($seconds -lt 1) { throw 'LEASE_EXPIRED_BEFORE_DELIVERY' }
    return [int]$seconds
}

function New-DocMindArtifactStreamContent {
    param(
        [Parameter(Mandatory = $true)][IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][ValidateRange(1, [Int64]::MaxValue)][Int64]$Length
    )
    $content = [Net.Http.StreamContent]::new($Stream)
    $content.Headers.ContentLength = $Length
    return $content
}

function Protect-DocMindJobDirectory {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $acl = Get-Acl -LiteralPath $LiteralPath
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $rules = @($acl.Access)
    $expectedSids = @($currentSid.Value, $systemSid.Value)
    $alreadyRestricted = $acl.AreAccessRulesProtected -and $rules.Count -eq 2
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $expectedSids -or $rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
            $rule.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit') {
            $alreadyRestricted = $false
        }
    }
    if ($alreadyRestricted) { return }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.Access | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($sid in @($currentSid, $systemSid)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, [Security.AccessControl.FileSystemRights]::FullControl, $inherit, $propagation, $allow)
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $LiteralPath -AclObject $acl
}

function Remove-DocMindExpiredJobDirectories {
    param(
        [Parameter(Mandatory = $true)][string]$WorkRoot,
        [ValidateRange(30, 86400)][int]$MinimumAgeSeconds = 300
    )
    if (-not (Test-Path -LiteralPath $WorkRoot -PathType Container)) { return 0 }
    Assert-DocMindNoReparsePoint -LiteralPath $WorkRoot -Boundary $WorkRoot -Name 'Work root'
    $removed = 0
    foreach ($directory in Get-ChildItem -LiteralPath $WorkRoot -Directory -Force) {
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        if ($directory.LastWriteTimeUtc -gt [DateTime]::UtcNow.AddSeconds(-$MinimumAgeSeconds)) { continue }
        $statePath = Join-Path $directory.FullName 'job-state.json'
        $leaseExpired = $true
        if (Test-Path -LiteralPath $statePath -PathType Leaf) {
            try {
                $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
                $leaseExpiry = [DateTimeOffset]::Parse([string]$state.lease_expires_at)
                $leaseExpired = $leaseExpiry -lt [DateTimeOffset]::UtcNow
            } catch { $leaseExpired = $false }
        }
        if (-not $leaseExpired) { continue }
        Assert-DocMindNoReparsePoint -LiteralPath $directory.FullName -Boundary $WorkRoot -Name 'Reaper target'
        try {
            Remove-Item -LiteralPath $directory.FullName -Recurse -Force -ErrorAction Stop
            $removed++
        } catch { }
    }
    return $removed
}
