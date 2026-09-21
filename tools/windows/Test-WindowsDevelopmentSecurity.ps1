[CmdletBinding()]
param(
    [string]$DockerCommand = 'docker',
    [string]$EnvironmentPath,
    [string]$DockerStoragePath,
    [switch]$RequireModelCredentials,
    [switch]$RequireEncryptedHostStorage
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
if (-not $EnvironmentPath) {
    $EnvironmentPath = Join-Path $localRoot 'docker/windows-dev.env'
}
$resolvedEnvironment = [IO.Path]::GetFullPath($EnvironmentPath)
if (-not $resolvedEnvironment.StartsWith($localRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Security validation requires an environment file below $localRoot"
}
if (-not (Test-Path -LiteralPath $resolvedEnvironment -PathType Leaf)) {
    throw "Missing local environment file: $resolvedEnvironment"
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            throw 'Malformed line in local environment file (value not displayed).'
        }
        $key = $Matches[1]
        if ($values.ContainsKey($key)) {
            throw "Duplicate key in local environment: $key"
        }
        $values[$key] = $Matches[2]
    }
    return $values
}

function Require-Exact($Values, [string]$Name, [string]$Expected) {
    if (-not $Values.ContainsKey($Name) -or $Values[$Name] -cne $Expected) {
        throw "Required development security policy is missing or invalid: $Name"
    }
}

function Require-Secret($Values, [string]$Name, [int]$MinimumLength) {
    if (-not $Values.ContainsKey($Name)) {
        throw "Required local secret is missing: $Name"
    }
    $value = [string]$Values[$Name]
    if ($value.Length -lt $MinimumLength) {
        throw "Required local secret is too short: $Name"
    }
    if ($value -match '(?i)GENERATED_LOCALLY|CHANGE_ME|PLACEHOLDER|EXAMPLE|PASSWORD') {
        throw "Required local secret contains a placeholder: $Name"
    }
}

$values = Read-DotEnv $resolvedEnvironment
Require-Exact $values 'DOCMIND_DEV_SECURITY_MODE' 'isolated-synthetic-only'
Require-Exact $values 'DOCMIND_DEV_DATA_CLASS' 'synthetic-only'
Require-Exact $values 'DOCMIND_DEV_ALLOW_PLAINTEXT_LOOPBACK' '1'
Require-Exact $values 'DOCMIND_DEV_EXTERNAL_API_POLICY' 'https-only'

foreach ($name in @('MYSQL_PASSWORD', 'ELASTIC_PASSWORD', 'REDIS_PASSWORD', 'MINIO_PASSWORD')) {
    Require-Secret $values $name 32
}
Require-Secret $values 'MINIO_USER' 16
$uniqueSecrets = @('MYSQL_PASSWORD', 'ELASTIC_PASSWORD', 'REDIS_PASSWORD', 'MINIO_PASSWORD') |
    ForEach-Object { [string]$values[$_] } |
    Sort-Object -Unique
if ($uniqueSecrets.Count -ne 4) {
    throw 'Infrastructure services must not share local development passwords.'
}

if ($RequireModelCredentials) {
    Require-Secret $values 'JINA_API_KEY' 12
    Require-Secret $values 'DASHSCOPE_API_KEY' 12
    if (-not $values.ContainsKey('DOCMIND_GENERATOR_MODEL') -or [string]::IsNullOrWhiteSpace($values['DOCMIND_GENERATOR_MODEL'])) {
        throw 'DOCMIND_GENERATOR_MODEL is required for the full profile.'
    }
}

$acl = Get-Acl -LiteralPath $resolvedEnvironment
if (-not $acl.AreAccessRulesProtected) {
    throw 'The local environment file still inherits broader directory permissions.'
}
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowedSids = @($currentSid, 'S-1-5-18')
foreach ($rule in $acl.Access) {
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $sid -notin $allowedSids) {
        throw "Unexpected principal can read the local environment file: $sid"
    }
}

$relativeEnvironment = $resolvedEnvironment.Substring(
    $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar).Length + 1
).Replace('\', '/')
Push-Location $repoRoot
try {
    & git check-ignore --quiet -- $relativeEnvironment
    if ($LASTEXITCODE -ne 0) {
        throw "Local environment is not ignored by Git: $relativeEnvironment"
    }
} finally {
    Pop-Location
}

& (Join-Path $PSScriptRoot 'Test-WindowsDevelopmentCompose.ps1') `
    -DockerCommand $DockerCommand `
    -EnvironmentPath $resolvedEnvironment

$storageMessage = 'Host storage encryption was not asserted; this profile remains synthetic-data-only.'
if ($RequireEncryptedHostStorage) {
    $command = Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'Cannot verify host storage encryption because Get-BitLockerVolume is unavailable.'
    }
    if (-not $DockerStoragePath) {
        throw 'DockerStoragePath is required so the Docker/WSL data location is checked, not only the env-file drive.'
    }
    $resolvedDockerStorage = [IO.Path]::GetFullPath($DockerStoragePath)
    if (-not (Test-Path -LiteralPath $resolvedDockerStorage)) {
        throw 'DockerStoragePath does not exist.'
    }
    $driveRoots = @(
        [IO.Path]::GetPathRoot($resolvedEnvironment).TrimEnd('\'),
        [IO.Path]::GetPathRoot($resolvedDockerStorage).TrimEnd('\')
    ) | Sort-Object -Unique
    foreach ($driveRoot in $driveRoots) {
        try {
            $bitLocker = Get-BitLockerVolume -MountPoint $driveRoot -ErrorAction Stop
        } catch {
            throw 'Cannot verify host storage encryption. Run this optional assertion from an elevated PowerShell session.'
        }
        if ($bitLocker.ProtectionStatus -ne 'On' -or $bitLocker.VolumeStatus -ne 'FullyEncrypted') {
            throw "Host storage encryption is not fully protected for $driveRoot."
        }
    }
    $storageMessage = "BitLocker protection is on and fully encrypted for the env-file and Docker-storage drive(s): $($driveRoots -join ', ')."
}

Write-Output 'Windows development security validation passed.'
Write-Output 'Verified: synthetic-only mode, unique non-placeholder secrets, restricted ACL, Git ignore, runtime gates, and loopback-only ports.'
Write-Output $storageMessage
