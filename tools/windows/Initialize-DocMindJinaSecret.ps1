[CmdletBinding()]
param(
    [string]$EnvironmentPath,
    [string]$SecretPath = 'C:\DocMindSecrets\jina-api.key',
    [switch]$RotateSecret
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..')).TrimEnd('\', '/')
if (-not $EnvironmentPath) { $EnvironmentPath = Join-Path $repoRoot '.local/docker/windows-dev.env' }
$environmentFile = [IO.Path]::GetFullPath($EnvironmentPath)
$secretFile = [IO.Path]::GetFullPath($SecretPath)
$repoPrefix = $repoRoot + [IO.Path]::DirectorySeparatorChar
if ($secretFile.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The Jina secret file must be stored outside the Git checkout.'
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw 'Existing Windows development environment file is missing.'
}
if ([IO.Path]::GetPathRoot($secretFile) -eq $secretFile) { throw 'The Jina secret path cannot be a drive root.' }

function Assert-NoReparsePoint([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'The Jina secret path must not traverse a reparse point.'
            }
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
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
    @($acl.Access) | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
    $allow = [Security.AccessControl.AccessControlType]::Allow
    [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($currentSid, [Security.AccessControl.FileSystemRights]::FullControl, $allow))
    [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($systemSid, ([Security.AccessControl.FileSystemRights]::Read -bor [Security.AccessControl.FileSystemRights]::Synchronize), $allow))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Convert-ToWslMountPath([string]$Path) {
    $root = [IO.Path]::GetPathRoot($Path)
    if ($root -notmatch '^([A-Za-z]):\\$') { throw 'The Jina secret must be on a local Windows drive available to WSL.' }
    $drive = $Matches[1].ToLowerInvariant()
    $tail = $Path.Substring($root.Length).Replace('\', '/')
    return "/mnt/$drive/$tail"
}

$mutex = [Threading.Mutex]::new($false, 'Global\DocMind-Jina-Secret-Initialize')
$acquired = $false
try {
    $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds(10))
    if (-not $acquired) { throw 'Another Jina secret initialization is already running.' }
    Assert-NoReparsePoint -Path (Split-Path -Parent $secretFile)

    $mustWrite = $RotateSecret -or -not (Test-Path -LiteralPath $secretFile -PathType Leaf)
    if ($mustWrite) {
        $secureValue = Read-Host 'Enter the Jina API key' -AsSecureString
        $pointer = [IntPtr]::Zero
        $plainValue = $null
        try {
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
            $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
            if ([string]::IsNullOrWhiteSpace($plainValue) -or $plainValue.Length -lt 12 -or
                $plainValue -match '[\r\n\x00]' -or $plainValue -match '(?i)CHANGE_ME|PLACEHOLDER|GENERATED_LOCALLY') {
                throw 'The supplied Jina API key is invalid.'
            }
            [IO.Directory]::CreateDirectory((Split-Path -Parent $secretFile)) | Out-Null
            $temporaryFile = "$secretFile.$([Guid]::NewGuid().ToString('n')).tmp"
            try {
                [IO.File]::WriteAllText($temporaryFile, $plainValue, [Text.UTF8Encoding]::new($false))
                Move-Item -LiteralPath $temporaryFile -Destination $secretFile -Force
            } finally {
                if (Test-Path -LiteralPath $temporaryFile) { Remove-Item -LiteralPath $temporaryFile -Force }
            }
        } finally {
            $plainValue = $null
            if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
            if ($secureValue) { $secureValue.Dispose() }
        }
    }

    $existingValue = [IO.File]::ReadAllText($secretFile, [Text.Encoding]::UTF8)
    try {
        if ($existingValue.Length -lt 12 -or $existingValue.Length -gt 16384 -or
            $existingValue -match '[\r\n\x00]' -or $existingValue -match '(?i)CHANGE_ME|PLACEHOLDER|GENERATED_LOCALLY') {
            throw 'The existing Jina secret file is invalid; use -RotateSecret.'
        }
    } finally {
        $existingValue = $null
    }
    Set-SecretAcl -Path $secretFile

    $replacements = [ordered]@{
        DOCMIND_JINA_SECRET_FILE = Convert-ToWslMountPath $secretFile
        DOCMIND_E2E_JINA_API_KEY_FILE = '/run/secrets/jina-api-key'
        JINA_API_KEY = ''
    }
    $updatedLines = [Collections.Generic.List[string]]::new()
    $seen = @{}
    foreach ($line in [IO.File]::ReadAllLines($environmentFile)) {
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
    foreach ($entry in $replacements.GetEnumerator()) {
        if (-not $seen.ContainsKey($entry.Key)) { $updatedLines.Add("$($entry.Key)=$($entry.Value)") }
    }
    $environmentTemp = "$environmentFile.$([Guid]::NewGuid().ToString('n')).tmp"
    try {
        [IO.File]::WriteAllLines($environmentTemp, $updatedLines, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $environmentTemp -Destination $environmentFile -Force
    } finally {
        if (Test-Path -LiteralPath $environmentTemp) { Remove-Item -LiteralPath $environmentTemp -Force }
    }
    Set-SecretAcl -Path $environmentFile
    Write-Output 'Jina credential file and Compose wiring are ready (secret value not logged).'
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
