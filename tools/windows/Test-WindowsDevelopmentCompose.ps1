[CmdletBinding()]
param(
    [string]$DockerCommand = 'docker',
    [string]$EnvironmentPath,
    [switch]$ApprovedDept2Reindex
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$composePath = Join-Path $repoRoot 'app/docker/docker-compose-windows-dev.yml'
if (-not $EnvironmentPath) {
    $EnvironmentPath = Join-Path $repoRoot '.local/docker/windows-dev.env'
}
$resolvedEnvironment = [IO.Path]::GetFullPath($EnvironmentPath)
if (-not (Test-Path -LiteralPath $resolvedEnvironment -PathType Leaf)) {
    throw "Missing local environment file. Run Initialize-WindowsDevelopmentDocker.ps1 first: $resolvedEnvironment"
}

Push-Location $repoRoot
try {
    $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedEnvironment.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Compose validation requires an environment file below the repository root: $repoRoot"
    }
    $relativeEnvironment = $resolvedEnvironment.Substring($repoPrefix.Length).Replace('\', '/')
    $relativeCompose = 'app/docker/docker-compose-windows-dev.yml'
    $composeArgs = @('compose', '--env-file', $relativeEnvironment, '-f', $relativeCompose)
    if ($ApprovedDept2Reindex) {
        $composeArgs += @('-f', 'app/docker/docker-compose-windows-dev-dept2-reindex.yml')
    }
    $composeArgs += @('--profile', 'full', 'config', '--format', 'json')
    $jsonText = (& $DockerCommand @composeArgs 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose config failed:`n$jsonText"
    }
    $config = $jsonText | ConvertFrom-Json
} finally {
    Pop-Location
}

if ($config.name -ne 'docmind-windows-dev') {
    throw "Unexpected Compose project name: $($config.name)"
}
$retiredCatalogService = 'open' + 'viking'
if ($config.services.PSObject.Properties.Name -contains $retiredCatalogService) {
    throw 'The retired catalog service must not be present in the Windows development stack.'
}
foreach ($gateName in @('security-gate', 'model-secret-gate')) {
    if ($config.services.PSObject.Properties.Name -notcontains $gateName) {
        throw "Missing fail-closed service: $gateName"
    }
    if ($config.services.$gateName.network_mode -ne 'none') {
        throw "Security gate must not have network access: $gateName"
    }
    if ($config.services.$gateName.image -notmatch '@sha256:[0-9a-f]{64}$') {
        throw "Security gate image must be digest-pinned: $gateName"
    }
    if (-not $config.services.$gateName.read_only -or
        $config.services.$gateName.cap_drop -notcontains 'ALL' -or
        $config.services.$gateName.security_opt -notcontains 'no-new-privileges:true') {
        throw "Security gate container hardening is incomplete: $gateName"
    }
}

$securityEnvironment = $config.services.'security-gate'.environment
$expectedMode = if ($ApprovedDept2Reindex) { 'isolated-approved-dept2-reindex' } else { 'isolated-synthetic-only' }
$expectedClass = if ($ApprovedDept2Reindex) { 'approved-dept2-reindex' } else { 'synthetic-only' }
if ($securityEnvironment.DOCMIND_DEV_SECURITY_MODE -ne $expectedMode -or
    $securityEnvironment.DOCMIND_DEV_DATA_CLASS -ne $expectedClass -or
    $securityEnvironment.DOCMIND_DEV_ALLOW_PLAINTEXT_LOOPBACK -ne '1' -or
    $securityEnvironment.DOCMIND_DEV_EXTERNAL_API_POLICY -ne 'https-only') {
    throw 'Resolved Compose does not enforce the selected development boundary.'
}
if ($ApprovedDept2Reindex -and $securityEnvironment.DOCMIND_DEV_APPROVED_SOURCE_ID -ne 'dept-2-e2e') {
    throw 'Resolved Compose does not enforce the approved DEPT2 source.'
}

$application = $config.services.'ragflow-cpu'
if ($application.environment.DOCMIND_HOST_WORKER_KEY_ID -ne 'windows-host-1' -or
    $application.environment.DOCMIND_HOST_WORKER_HMAC_SECRET_FILE -ne '/run/secrets/docmind-host-worker-hmac' -or
    $application.environment.DOCMIND_EPHEMERAL_PARSER_ROOT -ne '/run/docmind-ephemeral-parser') {
    throw 'Host-worker authentication or ephemeral parser environment is not wired into the application.'
}
$workerSecretMount = @($application.volumes | Where-Object { $_.target -eq '/run/secrets/docmind-host-worker-hmac' })
if ($workerSecretMount.Count -ne 1 -or -not $workerSecretMount[0].read_only -or $workerSecretMount[0].type -ne 'bind') {
    throw 'Host-worker HMAC secret must be a single read-only bind mount.'
}
$jinaSecretMount = @($application.volumes | Where-Object { $_.target -eq '/run/secrets/jina-api-key' })
if ($application.environment.JINA_API_KEY_FILE -ne '/run/secrets/jina-api-key' -or
    $jinaSecretMount.Count -ne 1 -or -not $jinaSecretMount[0].read_only -or $jinaSecretMount[0].type -ne 'bind') {
    throw 'Jina API credential must use a single read-only secret-file bind mount.'
}
$modelGateJinaMount = @($config.services.'model-secret-gate'.volumes | Where-Object { $_.target -eq '/run/secrets/jina-api-key' })
if ($config.services.'model-secret-gate'.environment.JINA_API_KEY_FILE -ne '/run/secrets/jina-api-key' -or
    $modelGateJinaMount.Count -ne 1 -or -not $modelGateJinaMount[0].read_only -or $modelGateJinaMount[0].type -ne 'bind') {
    throw 'Model credential gate must validate the same read-only Jina secret-file mount.'
}
$ephemeralTmpfs = @($application.tmpfs | Where-Object { [string]$_ -like '/run/docmind-ephemeral-parser:*' })
if ($ephemeralTmpfs.Count -ne 1 -or [string]$ephemeralTmpfs[0] -notmatch 'noexec' -or [string]$ephemeralTmpfs[0] -notmatch 'nosuid' -or [string]$ephemeralTmpfs[0] -notmatch 'nodev') {
    throw 'Ephemeral parser root must be a hardened tmpfs mount.'
}

$forbiddenRoots = @(
    'D:\UPLEXSOFT\UDRIVE\USER\TEST1',
    'D:\UPLEXSOFT\UDRIVE\DEPT\_DEPT_2',
    'D:\UPLEXSOFT\UDRIVE\DEPT\_DEPT_1',
    '/mnt/d/UPLEXSOFT/UDRIVE/USER/TEST1',
    '/mnt/d/UPLEXSOFT/UDRIVE/DEPT/_DEPT_2',
    '/mnt/d/UPLEXSOFT/UDRIVE/DEPT/_DEPT_1'
)
foreach ($serviceProperty in $config.services.PSObject.Properties) {
    $service = $serviceProperty.Value
    if ($service.PSObject.Properties.Name -contains 'container_name' -and $service.container_name) {
        throw "Fixed container_name found on service $($serviceProperty.Name)."
    }
    $ports = if ($service.PSObject.Properties.Name -contains 'ports') { @($service.ports) } else { @() }
    foreach ($port in $ports) {
        if ($port -and $port.host_ip -ne '127.0.0.1') {
            throw "Non-loopback published port found on service $($serviceProperty.Name): $($port | ConvertTo-Json -Compress)"
        }
    }
    $serviceVolumes = if ($service.PSObject.Properties.Name -contains 'volumes') { @($service.volumes) } else { @() }
    foreach ($volume in $serviceVolumes) {
        if (-not $volume) { continue }
        foreach ($root in $forbiddenRoots) {
            if ($volume.PSObject.Properties.Name -contains 'source' -and [string]$volume.source -like "$root*") {
                throw "Forbidden source-root mount found on service $($serviceProperty.Name)."
            }
        }
    }
}

$credentialOwners = @{
    JINA_API_KEY = @('model-secret-gate', 'ragflow-cpu')
    DASHSCOPE_API_KEY = @('model-secret-gate', 'ragflow-cpu')
    MYSQL_PASSWORD = @('security-gate', 'ragflow-cpu')
    MYSQL_ROOT_PASSWORD = @('mysql')
    ELASTIC_PASSWORD = @('es01', 'security-gate', 'ragflow-cpu')
    REDIS_PASSWORD = @('redis', 'security-gate', 'ragflow-cpu')
    MINIO_PASSWORD = @('security-gate', 'ragflow-cpu')
    MINIO_ROOT_PASSWORD = @('minio')
}
foreach ($credential in $credentialOwners.GetEnumerator()) {
    foreach ($serviceProperty in $config.services.PSObject.Properties) {
        $environmentNames = if ($serviceProperty.Value.PSObject.Properties.Name -contains 'environment') {
            @($serviceProperty.Value.environment.PSObject.Properties.Name)
        } else {
            @()
        }
        if ($environmentNames -contains $credential.Key -and $serviceProperty.Name -notin $credential.Value) {
            throw "Credential $($credential.Key) is exposed to unrelated service $($serviceProperty.Name)."
        }
    }
}

foreach ($volumeProperty in $config.volumes.PSObject.Properties) {
    if ($volumeProperty.Value.name -notlike 'docmind-windows-dev_*') {
        throw "Development volume escaped the isolated namespace: $($volumeProperty.Value.name)"
    }
}

$serialized = $config | ConvertTo-Json -Depth 100
if ($serialized -match "(?i)$retiredCatalogService") {
    throw 'Retired catalog configuration remains in the resolved Windows development stack.'
}
if ($serialized -match '(?i)uencryptor') {
    throw 'The Docker stack must not invoke or configure uEncryptor.'
}
Write-Output 'Windows development Compose validation passed.'
Write-Output 'Verified: isolated project, fail-closed security gates, scoped secrets, loopback-only published ports, no source-root mounts, and no retired catalog service.'
