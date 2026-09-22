[CmdletBinding()]
param(
    [string]$DockerCommand = 'docker',
    [string]$SecurityEnvironmentPath,
    [string]$DevelopmentEnvironmentPath,
    [ValidateRange(1, 365)]
    [int]$MaximumEvidenceAgeDays = 180,
    [switch]$CertificatePreflightOnly,
    [switch]$LifecycleEvidencePreflightOnly
)

$ErrorActionPreference = 'Stop'
if ($CertificatePreflightOnly -and $LifecycleEvidencePreflightOnly) {
    throw 'Choose only one preflight-only switch.'
}
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
$localSecurityRoot = [IO.Path]::GetFullPath((Join-Path $localRoot 'security'))
$dockerDirectory = Join-Path $repoRoot 'app/docker'
if (-not $SecurityEnvironmentPath) {
    $SecurityEnvironmentPath = Join-Path $localSecurityRoot 'windows-security-preflight.env'
}
if (-not $DevelopmentEnvironmentPath) {
    $DevelopmentEnvironmentPath = Join-Path $localRoot 'docker/windows-dev.env'
}
$resolvedSecurityEnvironment = [IO.Path]::GetFullPath($SecurityEnvironmentPath)
$resolvedDevelopmentEnvironment = [IO.Path]::GetFullPath($DevelopmentEnvironmentPath)
if (-not $resolvedSecurityEnvironment.StartsWith($localSecurityRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Security preflight configuration must stay below $localSecurityRoot"
}
if (-not (Test-Path -LiteralPath $resolvedSecurityEnvironment -PathType Leaf)) {
    throw "Missing security preflight configuration: $resolvedSecurityEnvironment"
}
if (-not (Test-Path -LiteralPath $resolvedDevelopmentEnvironment -PathType Leaf)) {
    throw "Missing Windows development environment: $resolvedDevelopmentEnvironment"
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            throw 'Malformed security preflight env line (value not displayed).'
        }
        $key = $Matches[1]
        if ($values.ContainsKey($key)) { throw "Duplicate security preflight key: $key" }
        $values[$key] = $Matches[2]
    }
    return $values
}

function Require-Value($Values, [string]$Name) {
    if (-not $Values.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace([string]$Values[$Name])) {
        throw "Missing security preflight setting: $Name"
    }
    return [string]$Values[$Name]
}

function Resolve-SecurityPath([string]$Value, [string]$Name) {
    $candidate = if ([IO.Path]::IsPathRooted($Value)) { $Value } else { Join-Path $dockerDirectory $Value }
    $resolved = [IO.Path]::GetFullPath($candidate)
    if (-not $resolved.StartsWith($localSecurityRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name must stay below the ignored local security directory."
    }
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Missing local security file: $Name"
    }
    return $resolved
}

function Assert-Ignored([string]$Path, [string]$Name) {
    $relative = $Path.Substring($repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar).Length + 1).Replace('\', '/')
    & git -C $repoRoot check-ignore --quiet -- $relative
    if ($LASTEXITCODE -ne 0) { throw "$Name is not ignored by Git." }
}

function Assert-RestrictedAcl([string]$Path, [string]$Name) {
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) { throw "$Name inherits broader directory permissions." }
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $sid -notin @($currentSid, 'S-1-5-18')) {
            throw "$Name is readable by an unexpected principal: $sid"
        }
    }
}

function Parse-UtcDate($Object, [string]$Name) {
    $raw = [string]$Object.$Name
    $parsed = [DateTimeOffset]::MinValue
    if ([string]::IsNullOrWhiteSpace($raw) -or -not [DateTimeOffset]::TryParse($raw, [ref]$parsed)) {
        throw "Evidence contains an invalid UTC timestamp: $Name"
    }
    if ($parsed.Offset -ne [TimeSpan]::Zero) { throw "Evidence timestamp must use UTC: $Name" }
    return $parsed
}

$values = Read-DotEnv $resolvedSecurityEnvironment
if ((Require-Value $values 'DOCMIND_SECURITY_PREFLIGHT_MODE') -cne 'certificate-prerequisite-only') {
    throw 'Security preflight mode must remain certificate-prerequisite-only.'
}
$caPath = Resolve-SecurityPath (Require-Value $values 'DOCMIND_TLS_CA_CERT_FILE') 'DOCMIND_TLS_CA_CERT_FILE'
$certificatePath = Resolve-SecurityPath (Require-Value $values 'DOCMIND_TLS_SERVER_CERT_FILE') 'DOCMIND_TLS_SERVER_CERT_FILE'
$privateKeyPath = Resolve-SecurityPath (Require-Value $values 'DOCMIND_TLS_SERVER_KEY_FILE') 'DOCMIND_TLS_SERVER_KEY_FILE'
if ($caPath -eq $certificatePath -or $certificatePath -eq $privateKeyPath -or $caPath -eq $privateKeyPath) {
    throw 'CA certificate, server certificate, and server private key must use separate files.'
}

foreach ($item in @(
    @{ Path = $resolvedSecurityEnvironment; Name = 'security preflight env' },
    @{ Path = $caPath; Name = 'CA certificate' },
    @{ Path = $certificatePath; Name = 'server certificate' },
    @{ Path = $privateKeyPath; Name = 'server private key' }
)) {
    Assert-Ignored $item.Path $item.Name
}
Assert-RestrictedAcl $resolvedSecurityEnvironment 'security preflight env'
Assert-RestrictedAcl $privateKeyPath 'server private key'

$securityRelative = $resolvedSecurityEnvironment.Substring($repoRoot.TrimEnd('\').Length + 1).Replace('\', '/')
$developmentRelative = $resolvedDevelopmentEnvironment.Substring($repoRoot.TrimEnd('\').Length + 1).Replace('\', '/')
Push-Location $repoRoot
try {
    $configText = (& $DockerCommand compose `
        --env-file $developmentRelative `
        --env-file $securityRelative `
        -f app/docker/docker-compose-windows-dev.yml `
        -f app/docker/docker-compose-windows-security-preflight.yml `
        --profile full `
        --profile security-preflight `
        config --format json 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "Security preflight Compose validation failed without starting containers.`n$configText" }
    $config = $configText | ConvertFrom-Json

    $gateOutput = (& $DockerCommand compose `
        --env-file $developmentRelative `
        --env-file $securityRelative `
        -f app/docker/docker-compose-windows-security-preflight.yml `
        --profile security-preflight `
        run --rm --no-deps production-security-preflight 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "Certificate prerequisite container failed:`n$gateOutput" }
} finally {
    Pop-Location
}

$gate = $config.services.'production-security-preflight'
if (-not $gate -or $gate.network_mode -ne 'none' -or -not $gate.read_only -or
    $gate.cap_drop -notcontains 'ALL' -or $gate.security_opt -notcontains 'no-new-privileges:true' -or
    $gate.image -notmatch '@sha256:[0-9a-f]{64}$' -or
    $gate.labels.'com.docmind.security-preflight-only' -ne 'true' -or
    $gate.labels.'com.docmind.production-tls-enabled' -ne 'false') {
    throw 'Certificate prerequisite gate is missing its non-production hardening labels or isolation.'
}
$gateMounts = @($gate.volumes)
foreach ($target in @('/security/ca.pem', '/security/server.pem', '/security/server.key')) {
    $mount = @($gateMounts | Where-Object { $_.target -eq $target })
    if ($mount.Count -ne 1 -or -not $mount[0].read_only -or $mount[0].type -ne 'bind') {
        throw "Certificate prerequisite mount must be one read-only bind: $target"
    }
}

Write-Output 'Certificate prerequisite passed: CA trust, server EKU/DNS, cert-key match, and minimum validity were verified.'
Write-Output 'This result does not assert runtime TLS, encrypted storage, rotation readiness, or external secret management.'
if ($CertificatePreflightOnly) { return }

# The certificate gate is deliberately independent from application services.
# Before accepting lifecycle evidence, verify that a separate Compose override
# has actually removed plaintext ingress/service links and environment secrets.
$blockers = [Collections.Generic.List[string]]::new()
$tlsServices = @($config.services.PSObject.Properties | Where-Object {
    $_.Value.labels -and $_.Value.labels.'com.docmind.production-tls-enabled' -eq 'true'
})
if ($tlsServices.Count -eq 0) { $blockers.Add('no production TLS termination/service profile is wired') }
$application = $config.services.'ragflow-cpu'
if (@($application.ports).Count -gt 0) { $blockers.Add('ragflow-cpu still publishes plaintext application ports directly') }
if ($config.services.es01.environment.'xpack.security.http.ssl.enabled' -ne 'true' -or
    $config.services.es01.environment.'xpack.security.transport.ssl.enabled' -ne 'true') {
    $blockers.Add('Elasticsearch HTTP/transport TLS is not enabled')
}
if ((@($config.services.mysql.command) -join ' ') -notmatch '--require_secure_transport(?:=|\s+)ON') {
    $blockers.Add('MySQL secure transport is not required')
}
$redisCommand = @($config.services.redis.command) -join ' '
if ($redisCommand -notmatch '--tls-port' -or $redisCommand -notmatch '(?:--port\s+0|--port=0)') {
    $blockers.Add('Valkey TLS-only transport is not configured')
}
$minioVolumes = @($config.services.minio.volumes)
if (-not ($minioVolumes | Where-Object { $_.target -like '*/certs*' })) {
    $blockers.Add('MinIO TLS certificate mounts are absent')
}
$environmentSecretNames = @('MYSQL_PASSWORD', 'ELASTIC_PASSWORD', 'REDIS_PASSWORD', 'MINIO_PASSWORD', 'JINA_API_KEY', 'DASHSCOPE_API_KEY')
$applicationEnvironmentNames = @($application.environment.PSObject.Properties.Name)
foreach ($name in $environmentSecretNames) {
    if ($applicationEnvironmentNames -contains $name) { $blockers.Add("application still receives $name through container environment") }
}
$secretProviderServices = @($config.services.PSObject.Properties | Where-Object {
    $_.Value.labels -and $_.Value.labels.'com.docmind.external-secret-provider' -eq 'true'
})
if ($secretProviderServices.Count -eq 0) {
    $blockers.Add('no externally managed secret-provider integration is wired')
}
if (-not $LifecycleEvidencePreflightOnly -and $blockers.Count -gt 0) {
    throw "Production security readiness failed before evidence review:`n - $($blockers -join "`n - ")"
}

$rotationEvidencePath = Resolve-SecurityPath (Require-Value $values 'DOCMIND_TLS_ROTATION_EVIDENCE_FILE') 'DOCMIND_TLS_ROTATION_EVIDENCE_FILE'
$secretEvidencePath = Resolve-SecurityPath (Require-Value $values 'DOCMIND_SECRET_PROVIDER_EVIDENCE_FILE') 'DOCMIND_SECRET_PROVIDER_EVIDENCE_FILE'
foreach ($item in @(
    @{ Path = $rotationEvidencePath; Name = 'TLS rotation evidence' },
    @{ Path = $secretEvidencePath; Name = 'secret-provider evidence' }
)) {
    Assert-Ignored $item.Path $item.Name
    Assert-RestrictedAcl $item.Path $item.Name
}

$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($certificatePath)
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $certificateSha256 = ([BitConverter]::ToString($sha256.ComputeHash($certificate.RawData)) -replace '-', '').ToLowerInvariant()
} finally {
    $sha256.Dispose()
}
$rotation = Get-Content -LiteralPath $rotationEvidencePath -Raw | ConvertFrom-Json
if ($rotation.schema_version -ne 1 -or [string]$rotation.certificate_sha256 -cne $certificateSha256) {
    throw 'TLS rotation evidence does not identify the validated server certificate.'
}
$now = [DateTimeOffset]::UtcNow
$evidenceCutoff = $now.AddDays(-$MaximumEvidenceAgeDays)
$rotatedAt = Parse-UtcDate $rotation 'rotated_at_utc'
$trustTestedAt = Parse-UtcDate $rotation 'trust_tested_at_utc'
$rollbackTestedAt = Parse-UtcDate $rotation 'rollback_tested_at_utc'
$nextRotationDue = Parse-UtcDate $rotation 'next_rotation_due_utc'
if ($rotatedAt -gt $now -or $trustTestedAt -lt $evidenceCutoff -or $trustTestedAt -gt $now -or
    $rollbackTestedAt -lt $evidenceCutoff -or $rollbackTestedAt -gt $now -or
    $nextRotationDue -le $now -or $nextRotationDue -ge [DateTimeOffset]$certificate.NotAfter.ToUniversalTime()) {
    throw 'TLS rotation/trust/rollback evidence is stale, future-dated, or inconsistent with certificate expiry.'
}
if ([string]::IsNullOrWhiteSpace([string]$rotation.owner) -or [string]::IsNullOrWhiteSpace([string]$rotation.runbook_reference)) {
    throw 'TLS rotation evidence must identify an owner and a runbook reference.'
}

$secretEvidence = Get-Content -LiteralPath $secretEvidencePath -Raw | ConvertFrom-Json
$secretAttestedAt = Parse-UtcDate $secretEvidence 'attested_at_utc'
$secretRotationTestedAt = Parse-UtcDate $secretEvidence 'rotation_tested_at_utc'
if ($secretEvidence.schema_version -ne 1 -or
    [string]::IsNullOrWhiteSpace([string]$secretEvidence.provider) -or
    [string]::IsNullOrWhiteSpace([string]$secretEvidence.application_identity) -or
    [string]::IsNullOrWhiteSpace([string]$secretEvidence.break_glass_runbook) -or
    $secretEvidence.separates_database_credentials -ne $true -or
    $secretEvidence.separates_model_api_credentials -ne $true -or
    $secretEvidence.secrets_not_in_environment -ne $true -or
    $secretAttestedAt -lt $evidenceCutoff -or $secretAttestedAt -gt $now -or
    $secretRotationTestedAt -lt $evidenceCutoff -or $secretRotationTestedAt -gt $now) {
    throw 'External secret-provider separation/rotation evidence is incomplete or stale.'
}

if ($LifecycleEvidencePreflightOnly) {
    Write-Output 'Lifecycle evidence prerequisite passed: certificate identity, rotation/trust/rollback dates, and secret-provider attestations are internally consistent.'
    Write-Output 'This result does not assert runtime TLS wiring, live host storage encryption, or production readiness.'
    return
}

$dockerStorageValue = Require-Value $values 'DOCMIND_DOCKER_STORAGE_PATH'
$dockerStoragePath = [IO.Path]::GetFullPath($dockerStorageValue)
if (-not (Test-Path -LiteralPath $dockerStoragePath)) { throw 'The configured Docker/WSL storage path does not exist.' }
$bitLockerCommand = Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue
if (-not $bitLockerCommand) { throw 'Get-BitLockerVolume is unavailable; host storage encryption cannot be verified.' }
$storagePaths = @($resolvedSecurityEnvironment, $privateKeyPath, $dockerStoragePath)
$driveRoots = $storagePaths | ForEach-Object { [IO.Path]::GetPathRoot($_).TrimEnd('\') } | Sort-Object -Unique
foreach ($driveRoot in $driveRoots) {
    try {
        $bitLocker = Get-BitLockerVolume -MountPoint $driveRoot -ErrorAction Stop
    } catch {
        throw 'Host storage encryption requires an elevated PowerShell session and live BitLocker inspection.'
    }
    if ($bitLocker.ProtectionStatus -ne 'On' -or $bitLocker.VolumeStatus -ne 'FullyEncrypted') {
        throw "Host storage encryption is not fully protected for $driveRoot."
    }
}

Write-Output 'Production security readiness passed: runtime TLS, certificate lifecycle evidence, encrypted host storage, and external secret separation were verified.'
