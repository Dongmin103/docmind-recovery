Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

function New-DocMindRandomBytes {
    param([ValidateRange(1, 1024)][int]$Count)
    $bytes = [byte[]]::new($Count)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return ,$bytes
}

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

function Resolve-DocMindSourceCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    if ([IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Contains(':') -or $RelativePath.Contains([char]0)) { throw 'relative_path must be a relative filesystem path.' }
    $segments = $RelativePath -split '[\\/]'
    if ($segments.Count -eq 0 -or @($segments | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) { throw 'relative_path contains an unsafe segment.' }
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $rootPath -PathType Container)) { throw 'SOURCE_ROOT_UNAVAILABLE' }
    Assert-DocMindNoReparsePoint -LiteralPath $rootPath -Boundary $rootPath -Name 'Source root'
    $candidate = [IO.Path]::GetFullPath((Join-Path $rootPath ($segments -join [IO.Path]::DirectorySeparatorChar)))
    Assert-DocMindNoReparsePoint -LiteralPath $candidate -Boundary $rootPath -Name 'Source candidate'
    return $candidate
}

function Test-DocMindConfirmedSourceFileAbsence {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidate = Resolve-DocMindSourceCandidate -Root $rootPath -RelativePath $RelativePath
    if (Test-Path -LiteralPath $candidate) { return $false }

    # A missing descendant is authoritative only when the closest existing
    # ancestor can actually be enumerated.  Test-Path alone can turn a transient
    # disconnect or access denial into a false deletion.
    $ancestor = Split-Path -Parent $candidate
    while (-not (Test-Path -LiteralPath $ancestor -PathType Container)) {
        if ($ancestor.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase)) { throw 'SOURCE_ROOT_UNAVAILABLE' }
        $parent = Split-Path -Parent $ancestor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent.Equals($ancestor, [StringComparison]::OrdinalIgnoreCase)) { throw 'SOURCE_ROOT_UNAVAILABLE' }
        $ancestor = $parent
    }
    Assert-DocMindNoReparsePoint -LiteralPath $ancestor -Boundary $rootPath -Name 'Deletion confirmation ancestor'
    try {
        $enumerator = [IO.Directory]::EnumerateFileSystemEntries($ancestor).GetEnumerator()
        try { [void]$enumerator.MoveNext() } finally { if ($enumerator -is [IDisposable]) { $enumerator.Dispose() } }
    } catch { throw 'SOURCE_ANCESTOR_ACCESS_FAILED' }
    if (Test-Path -LiteralPath $candidate) { return $false }
    return $true
}

function Select-DocMindConfiguredSources {
    param(
        [Parameter(Mandatory = $true)][object[]]$Sources,
        [string]$SourceId
    )
    if ([string]::IsNullOrWhiteSpace($SourceId)) { return $Sources }
    Assert-DocMindIdentifier -Value $SourceId -Name 'source_id'
    $selected = @($Sources | Where-Object { [string]$_.source_id -eq $SourceId })
    if ($selected.Count -ne 1) { throw 'Exactly one configured source must match -SourceId.' }
    return $selected
}

function Get-DocMindSourceReadFailureCode {
    param([Parameter(Mandatory = $true)][string]$LiteralPath, [Parameter(Mandatory = $true)]$ErrorRecord)
    $kind = switch ([IO.Path]::GetExtension($LiteralPath).ToLowerInvariant()) {
        '.doc' { 'DOC' }
        '.db' { 'DB' }
        '.leo_drive_usage' { 'USAGE' }
        default { 'OTHER' }
    }
    $exception = $ErrorRecord.Exception
    while ($null -ne $exception.InnerException) { $exception = $exception.InnerException }
    return ('SOURCE_FILE_READ_FAILED_{0}_{1:X8}' -f $kind, [uint32]($exception.HResult -band 0xffffffffL))
}

function Test-DocMindDiscoveryScanDue {
    param(
        [Parameter(Mandatory = $true)][DateTimeOffset]$Now,
        [bool]$EventDue,
        [bool]$FallbackDue,
        $FailureNotBefore
    )
    if (-not $EventDue -and -not $FallbackDue) { return $false }
    return $null -eq $FailureNotBefore -or $Now -ge [DateTimeOffset]$FailureNotBefore
}

function Get-DocMindSourceSnapshotEntries {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $rootPath -PathType Container)) { throw 'SOURCE_ROOT_UNAVAILABLE' }
    Assert-DocMindNoReparsePoint -LiteralPath $rootPath -Boundary $rootPath -Name 'Source root'
    $rootPrefix = $rootPath + [IO.Path]::DirectorySeparatorChar
    $pending = [Collections.Generic.Stack[string]]::new()
    $directoryStates = [Collections.Generic.List[object]]::new()
    $pending.Push($rootPath)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        Assert-DocMindNoReparsePoint -LiteralPath $directory -Boundary $rootPath -Name 'Scan directory'
        try { $directoryBefore = Get-Item -LiteralPath $directory -Force -ErrorAction Stop } catch { throw 'SOURCE_ENUMERATION_FAILED' }
        $directoryStates.Add([pscustomobject]@{ path = $directory; last_write_ticks = [Int64]$directoryBefore.LastWriteTimeUtc.Ticks })
        try { $children = @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) } catch { throw 'SOURCE_ENUMERATION_FAILED' }
        foreach ($child in $children) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'SOURCE_REPARSE_POINT_FOUND' }
            if ($child.PSIsContainer) { $pending.Push($child.FullName); continue }
            if (-not $child.FullName.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'SOURCE_PATH_ESCAPED' }
            $relativePath = $child.FullName.Substring($rootPrefix.Length).Replace('\', '/')
            $beforeLength = [Int64]$child.Length
            $beforeTicks = [Int64]$child.LastWriteTimeUtc.Ticks
            try { $hashBefore = Get-DocMindFileSha256 -LiteralPath $child.FullName } catch { throw (Get-DocMindSourceReadFailureCode -LiteralPath $child.FullName -ErrorRecord $_) }
            try { $after = Get-Item -LiteralPath $child.FullName -Force -ErrorAction Stop } catch { throw 'SOURCE_FILE_CHANGED_DURING_SCAN' }
            if ($after.PSIsContainer -or [Int64]$after.Length -ne $beforeLength -or [Int64]$after.LastWriteTimeUtc.Ticks -ne $beforeTicks) { throw 'SOURCE_FILE_CHANGED_DURING_SCAN' }
            try { $hashAfter = Get-DocMindFileSha256 -LiteralPath $child.FullName } catch { throw (Get-DocMindSourceReadFailureCode -LiteralPath $child.FullName -ErrorRecord $_) }
            if (-not (Test-DocMindFixedTimeHexEqual $hashBefore $hashAfter)) { throw 'SOURCE_FILE_CHANGED_DURING_SCAN' }
            [pscustomobject][ordered]@{
                relative_path = $relativePath
                ciphertext_sha256 = $hashAfter
                size = [Int64]$after.Length
                mtime_ns = ([Int64]$after.LastWriteTimeUtc.Ticks - 621355968000000000L) * 100L
            }
        }
    }
    foreach ($state in $directoryStates) {
        try { $directoryAfter = Get-Item -LiteralPath $state.path -Force -ErrorAction Stop } catch { throw 'SOURCE_CHANGED_DURING_SCAN' }
        if (-not $directoryAfter.PSIsContainer -or ($directoryAfter.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or [Int64]$directoryAfter.LastWriteTimeUtc.Ticks -ne [Int64]$state.last_write_ticks) {
            throw 'SOURCE_CHANGED_DURING_SCAN'
        }
    }
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
    $expires = ConvertTo-DocMindDateTimeOffset -Value $Payload.lease_expires_at -Name 'lease_expires_at'
    if ($expires -le [DateTimeOffset]::UtcNow.AddSeconds($MinimumRemainingSeconds)) { throw 'Signed lease is expired or too close to expiry.' }
    if ([IO.Path]::IsPathRooted([string]$Payload.relative_path) -or ([string]$Payload.relative_path).Contains(':')) { throw 'Signed lease contains a physical path.' }
    return $expires.ToUniversalTime()
}

function ConvertTo-DocMindDateTimeOffset {
    param([Parameter(Mandatory = $true)]$Value, [string]$Name = 'timestamp')
    if ($Value -is [DateTimeOffset]) { return ([DateTimeOffset]$Value).ToUniversalTime() }
    if ($Value -is [DateTime]) {
        $dateTime = [DateTime]$Value
        if ($dateTime.Kind -eq [DateTimeKind]::Unspecified) { throw "$Name is invalid." }
        return ([DateTimeOffset]$dateTime).ToUniversalTime()
    }
    $parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse([string]$Value, [ref]$parsed)) { throw "$Name is invalid." }
    return $parsed.ToUniversalTime()
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

function Write-DocMindCleanupReceiptAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptRoot,
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)][string]$VersionId,
        [Parameter(Mandatory = $true)][Int64]$FencingToken,
        [Parameter(Mandatory = $true)][ValidateSet('CLEANUP_PENDING', 'CLEANED', 'FAILED')][string]$FinalState,
        [AllowNull()][string]$ErrorCode
    )
    Assert-DocMindIdentifier -Value $JobId -Name 'job_id'
    Assert-DocMindIdentifier -Value $VersionId -Name 'version_id'
    if ($FencingToken -lt 1) { throw 'Cleanup receipt fencing_token is invalid.' }
    if (-not [string]::IsNullOrEmpty($ErrorCode) -and $ErrorCode -notmatch '^[A-Z0-9_]{1,64}$') { throw 'Cleanup receipt error_code is invalid.' }
    $normalizedErrorCode = if ([string]::IsNullOrEmpty($ErrorCode)) { $null } else { $ErrorCode }
    $root = [IO.Path]::GetFullPath($ReceiptRoot).TrimEnd('\', '/')
    [IO.Directory]::CreateDirectory($root) | Out-Null
    Protect-DocMindJobDirectory -LiteralPath $root
    Assert-DocMindNoReparsePoint -LiteralPath $root -Boundary $root -Name 'Cleanup receipt root'
    $identityBytes = [Text.Encoding]::UTF8.GetBytes(('{0}`n{1}`n{2}' -f $JobId, $VersionId, $FencingToken))
    $name = (Get-DocMindBytesSha256 -Bytes $identityBytes) + '.json'
    $destination = Join-Path $root $name
    $receipt = [ordered]@{
        job_id = $JobId
        version_id = $VersionId
        fencing_token = $FencingToken
        final_state = $FinalState
        error_code = $normalizedErrorCode
    }
    $json = $receipt | ConvertTo-Json -Compress
    $replaceExisting = $false
    if (Test-Path -LiteralPath $destination -PathType Leaf) {
        $existing = Read-DocMindCleanupReceipt -LiteralPath $destination -ReceiptRoot $root
        if ([string]$existing.job_id -ne $JobId -or [string]$existing.version_id -ne $VersionId -or [Int64]$existing.fencing_token -ne $FencingToken) {
            throw 'CLEANUP_RECEIPT_CONFLICT'
        }
        if ([string]$existing.final_state -eq $FinalState -and [string]$existing.error_code -eq [string]$normalizedErrorCode) { return $destination }
        if ([string]$existing.final_state -ne 'CLEANUP_PENDING' -or $FinalState -eq 'CLEANUP_PENDING') { throw 'CLEANUP_RECEIPT_CONFLICT' }
        $replaceExisting = $true
    }
    $temporary = Join-Path $root ('.receipt-{0}.tmp' -f [Guid]::NewGuid().ToString('n'))
    $backup = $null
    try {
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally { $stream.Dispose() }
        if ($replaceExisting) {
            $backup = Join-Path $root ('.receipt-{0}.bak' -f [Guid]::NewGuid().ToString('n'))
            [IO.File]::Replace($temporary, $destination, $backup, $true)
        } else {
            Move-Item -LiteralPath $temporary -Destination $destination -ErrorAction Stop
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
        if ($null -ne $backup -and (Test-Path -LiteralPath $backup)) { Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue }
    }
    return $destination
}

function Read-DocMindCleanupReceipt {
    param([Parameter(Mandatory = $true)][string]$LiteralPath, [Parameter(Mandatory = $true)][string]$ReceiptRoot)
    Assert-DocMindNoReparsePoint -LiteralPath $LiteralPath -Boundary $ReceiptRoot -Name 'Cleanup receipt'
    $receipt = Get-Content -LiteralPath $LiteralPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    $expected = @('job_id', 'version_id', 'fencing_token', 'final_state', 'error_code')
    $actual = @($receipt.PSObject.Properties.Name)
    if (@($actual | Where-Object { $_ -notin $expected }).Count -gt 0 -or @($expected | Where-Object { $_ -notin $actual }).Count -gt 0) { throw 'CLEANUP_RECEIPT_SCHEMA_INVALID' }
    Assert-DocMindIdentifier -Value ([string]$receipt.job_id) -Name 'job_id'
    Assert-DocMindIdentifier -Value ([string]$receipt.version_id) -Name 'version_id'
    $fence = 0L
    if (-not [Int64]::TryParse([string]$receipt.fencing_token, [ref]$fence) -or $fence -lt 1) { throw 'CLEANUP_RECEIPT_SCHEMA_INVALID' }
    if ([string]$receipt.final_state -notin @('CLEANUP_PENDING', 'CLEANED', 'FAILED')) { throw 'CLEANUP_RECEIPT_SCHEMA_INVALID' }
    if ($null -ne $receipt.error_code -and [string]$receipt.error_code -notmatch '^[A-Z0-9_]{1,64}$') { throw 'CLEANUP_RECEIPT_SCHEMA_INVALID' }
    return $receipt
}

function Invoke-DocMindCleanupReceiptReplay {
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptRoot,
        [string]$WorkRoot,
        [Parameter(Mandatory = $true)][scriptblock]$Sender
    )
    $root = [IO.Path]::GetFullPath($ReceiptRoot).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { return [pscustomobject]@{ acknowledged = 0; pending = 0 } }
    Assert-DocMindNoReparsePoint -LiteralPath $root -Boundary $root -Name 'Cleanup receipt root'
    $acknowledged = 0
    $pending = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $root -File -Filter '*.json' -Force -ErrorAction Stop)) {
        try {
            $receipt = Read-DocMindCleanupReceipt -LiteralPath $file.FullName -ReceiptRoot $root
            if ([string]$receipt.final_state -eq 'CLEANUP_PENDING') {
                if ([string]::IsNullOrWhiteSpace($WorkRoot)) { $pending++; continue }
                $work = [IO.Path]::GetFullPath($WorkRoot).TrimEnd('\', '/')
                Assert-DocMindNoReparsePoint -LiteralPath $work -Boundary $work -Name 'Work root'
                $liveDirectory = @(Get-ChildItem -LiteralPath $work -Directory -Filter (('{0}-*' -f [string]$receipt.job_id)) -Force -ErrorAction Stop).Count -gt 0
                if ($liveDirectory) { $pending++; continue }
                [void](Write-DocMindCleanupReceiptAtomic -ReceiptRoot $root -JobId ([string]$receipt.job_id) -VersionId ([string]$receipt.version_id) -FencingToken ([Int64]$receipt.fencing_token) -FinalState 'CLEANED' -ErrorCode $null)
                $receipt = Read-DocMindCleanupReceipt -LiteralPath $file.FullName -ReceiptRoot $root
            }
            & $Sender $receipt
            Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
            $acknowledged++
        } catch { $pending++ }
    }
    return [pscustomobject]@{ acknowledged = $acknowledged; pending = $pending }
}

function Remove-DocMindExpiredJobDirectories {
    param(
        [Parameter(Mandatory = $true)][string]$WorkRoot,
        [string]$ReceiptRoot,
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
        $state = $null
        if (Test-Path -LiteralPath $statePath -PathType Leaf) {
            try {
                $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
                $leaseExpiry = ConvertTo-DocMindDateTimeOffset -Value $state.lease_expires_at -Name 'lease_expires_at'
                $leaseExpired = $leaseExpiry -lt [DateTimeOffset]::UtcNow
            } catch { $leaseExpired = $false }
        }
        if (-not $leaseExpired) { continue }
        Assert-DocMindNoReparsePoint -LiteralPath $directory.FullName -Boundary $WorkRoot -Name 'Reaper target'
        try {
            if ($null -ne $state -and -not [string]::IsNullOrWhiteSpace($ReceiptRoot)) {
                [void](Write-DocMindCleanupReceiptAtomic -ReceiptRoot $ReceiptRoot -JobId ([string]$state.job_id) -VersionId ([string]$state.version_id) -FencingToken ([Int64]$state.fencing_token) -FinalState 'CLEANUP_PENDING' -ErrorCode $null)
            }
            Remove-Item -LiteralPath $directory.FullName -Recurse -Force -ErrorAction Stop
            if ($null -ne $state -and -not [string]::IsNullOrWhiteSpace($ReceiptRoot)) {
                [void](Write-DocMindCleanupReceiptAtomic -ReceiptRoot $ReceiptRoot -JobId ([string]$state.job_id) -VersionId ([string]$state.version_id) -FencingToken ([Int64]$state.fencing_token) -FinalState 'CLEANED' -ErrorCode $null)
            }
            $removed++
        } catch { }
    }
    return $removed
}
