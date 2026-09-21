[CmdletBinding()]
param(
    [string]$EnvironmentPath,
    [string]$SecretPath,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
    [string]$KeyId = 'windows-host-1',
    [switch]$RotateSecret
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local')).TrimEnd('\', '/')
$composeRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot 'app/docker')).TrimEnd('\', '/')
if (-not $EnvironmentPath) { $EnvironmentPath = Join-Path $localRoot 'docker/windows-dev.env' }
if (-not $SecretPath) { $SecretPath = Join-Path $localRoot 'docker/host-worker-hmac.key' }
$environmentFile = [IO.Path]::GetFullPath($EnvironmentPath)
$secretFile = [IO.Path]::GetFullPath($SecretPath)
$localPrefix = $localRoot + [IO.Path]::DirectorySeparatorChar
foreach ($candidate in @($environmentFile, $secretFile)) {
    if (-not $candidate.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Host-worker local files must stay below the repository .local directory.' }
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) { throw 'Existing Windows development environment file is missing.' }

function Assert-NoReparsePath([string]$Path, [string]$Boundary) {
    $current = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($Boundary).TrimEnd('\', '/')
    while ($current.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Host-worker local files must not traverse a reparse point.' }
        }
        if ($current.Equals($root, [StringComparison]::OrdinalIgnoreCase)) { break }
        $current = Split-Path -Parent $current
    }
}

function Set-SecretAcl([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $rules = @($acl.Access)
    $currentRule = @($rules | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $currentSid.Value })
    $systemRule = @($rules | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $systemSid.Value })
    $alreadyRestricted = $acl.AreAccessRulesProtected -and $rules.Count -eq 2 -and
        $currentRule.Count -eq 1 -and $currentRule[0].AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $currentRule[0].FileSystemRights -eq [Security.AccessControl.FileSystemRights]::FullControl -and
        $systemRule.Count -eq 1 -and $systemRule[0].AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $systemRule[0].FileSystemRights -eq ([Security.AccessControl.FileSystemRights]::Read -bor [Security.AccessControl.FileSystemRights]::Synchronize)
    if ($alreadyRestricted) { return }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.Access | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($entry in @(
        @($currentSid, [Security.AccessControl.FileSystemRights]::FullControl),
        @($systemSid, [Security.AccessControl.FileSystemRights]::Read)
    )) {
        [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($entry[0], $entry[1], $allow))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$mutex = [Threading.Mutex]::new($false, 'Global\DocMind-HostWorker-Secret-Initialize')
$acquired = $false
try {
    $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds(10))
    if (-not $acquired) { throw 'Another host-worker secret initialization is already running.' }
    Assert-NoReparsePath -Path $environmentFile -Boundary $localRoot
    Assert-NoReparsePath -Path (Split-Path -Parent $secretFile) -Boundary $localRoot

    if ((Test-Path -LiteralPath $secretFile -PathType Leaf) -and -not $RotateSecret) {
        if ((Get-Item -LiteralPath $secretFile).Length -ne 32) { throw 'Existing host-worker secret is not exactly 32 bytes; explicit rotation is required.' }
    } else {
        [IO.Directory]::CreateDirectory((Split-Path -Parent $secretFile)) | Out-Null
        $secretBytes = [byte[]]::new(32)
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($secretBytes) } finally { $generator.Dispose() }
        $secretTemp = "$secretFile.$([Guid]::NewGuid().ToString('n')).tmp"
        try {
            [IO.File]::WriteAllBytes($secretTemp, $secretBytes)
            Move-Item -LiteralPath $secretTemp -Destination $secretFile -Force
        } finally {
            if (Test-Path -LiteralPath $secretTemp) { Remove-Item -LiteralPath $secretTemp -Force }
        }
    }
    Set-SecretAcl -Path $secretFile

    $composeBase = [Uri]::new($composeRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar)
    $secretUri = [Uri]::new($secretFile)
    $composeSecretPath = [Uri]::UnescapeDataString($composeBase.MakeRelativeUri($secretUri).ToString())
    if ([string]::IsNullOrWhiteSpace($composeSecretPath) -or $composeSecretPath.Contains("`n") -or $composeSecretPath.Contains('=')) { throw 'Unable to construct a safe Compose-relative secret path.' }

    $replacements = [ordered]@{
        DOCMIND_HOST_WORKER_KEY_ID = $KeyId
        DOCMIND_HOST_WORKER_SECRET_FILE = $composeSecretPath
    }
    $originalLines = [IO.File]::ReadAllLines($environmentFile)
    $seen = @{}
    $updatedLines = [Collections.Generic.List[string]]::new()
    foreach ($line in $originalLines) {
        $matched = $false
        foreach ($entry in $replacements.GetEnumerator()) {
            if ($line -match ('^{0}=' -f [Regex]::Escape($entry.Key))) {
                if (-not $seen.ContainsKey($entry.Key)) {
                    $updatedLines.Add("$($entry.Key)=$($entry.Value)")
                    $seen[$entry.Key] = $true
                }
                $matched = $true
                break
            }
        }
        if (-not $matched) { $updatedLines.Add($line) }
    }
    foreach ($entry in $replacements.GetEnumerator()) { if (-not $seen.ContainsKey($entry.Key)) { $updatedLines.Add("$($entry.Key)=$($entry.Value)") } }
    $environmentTemp = "$environmentFile.$([Guid]::NewGuid().ToString('n')).tmp"
    try {
        [IO.File]::WriteAllLines($environmentTemp, $updatedLines, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $environmentTemp -Destination $environmentFile -Force
    } finally {
        if (Test-Path -LiteralPath $environmentTemp) { Remove-Item -LiteralPath $environmentTemp -Force }
    }
    & (Join-Path $PSScriptRoot 'Initialize-WindowsDevelopmentSecurity.ps1') -EnvironmentPath $environmentFile
    Write-Output 'Host-worker HMAC secret and existing Windows development environment wiring are ready (secret value not logged).'
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
