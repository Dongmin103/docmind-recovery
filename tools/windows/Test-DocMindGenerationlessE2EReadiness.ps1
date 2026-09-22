[CmdletBinding()]
param(
    [string]$DockerCommand = 'docker',
    [string]$EnvFile,
    [string]$HostWorkerConfigPath = $env:DOCMIND_HOST_WORKER_CONFIG
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $repoRoot '.local/docker/windows-dev.env'
}
$envPath = [IO.Path]::GetFullPath($EnvFile)
$overlayPath = Join-Path $repoRoot 'app/docker/docker-compose-windows-dev-generationless-e2e.yml'
$reportRoot = Join-Path $repoRoot '.local/uEncryptor2/reports'
$jobRoot = Join-Path $repoRoot '.local/uEncryptor2/jobs'
$expectedAppImageId = 'sha256:7c14804c8325729a8f72a5310518d8b0b206cd106ff25c2a4bc15b635f29c9dc'
$blockers = [Collections.Generic.List[string]]::new()

function Add-Blocker([string]$Code) {
    if (-not $blockers.Contains($Code)) { $blockers.Add($Code) }
}

function Read-EnvFile([string]$LiteralPath) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) { return $values }
    foreach ($line in Get-Content -LiteralPath $LiteralPath) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $values[$Matches[1]] = $Matches[2]
        }
    }
    return $values
}

function Test-UsableSecret([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -lt 12) { return $false }
    return $Value -notmatch 'CHANGE_ME|PLACEHOLDER|GENERATED_LOCALLY'
}

function Get-ImageId([string]$Image) {
    try {
        $raw = & $DockerCommand image inspect $Image 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
        $rows = $raw | ConvertFrom-Json
        return [string]@($rows)[0].Id
    } catch {
        return $null
    }
}

function Get-ContainerHealth([string]$Name) {
    try {
        $raw = & $DockerCommand inspect $Name 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) { return 'missing' }
        $row = @($raw | ConvertFrom-Json)[0]
        if (-not $row.State.Running) { return 'stopped' }
        if ($null -ne $row.State.Health) { return [string]$row.State.Health.Status }
        return 'running'
    } catch {
        return 'missing'
    }
}

$envValues = Read-EnvFile -LiteralPath $envPath
$jinaKey = [Environment]::GetEnvironmentVariable('JINA_API_KEY')
if ([string]::IsNullOrWhiteSpace($jinaKey) -and $envValues.ContainsKey('JINA_API_KEY')) {
    $jinaKey = [string]$envValues['JINA_API_KEY']
}
$jinaReady = Test-UsableSecret $jinaKey
$jinaKey = $null
if (-not $jinaReady) { Add-Blocker 'JINA_API_KEY_NOT_AVAILABLE_TO_E2E_PROCESS' }

$workerSecretReady = $false
if ($envValues.ContainsKey('DOCMIND_HOST_WORKER_SECRET_FILE')) {
    $candidate = [string]$envValues['DOCMIND_HOST_WORKER_SECRET_FILE']
    if (-not [IO.Path]::IsPathRooted($candidate)) { $candidate = Join-Path (Join-Path $repoRoot 'app/docker') $candidate }
    try {
        $workerSecretReady = (Get-Item -LiteralPath ([IO.Path]::GetFullPath($candidate)) -ErrorAction Stop).Length -eq 32
    } catch {
        $workerSecretReady = $false
    }
}
if (-not $workerSecretReady) { Add-Blocker 'HOST_WORKER_HMAC_SECRET_MISSING_OR_INVALID' }

$hostConfigReady = $false
$hostSourceCount = 0
if ([string]::IsNullOrWhiteSpace($HostWorkerConfigPath)) {
    Add-Blocker 'HOST_WORKER_CONFIG_MISSING'
} else {
    try {
        $hostConfigFile = [IO.Path]::GetFullPath($HostWorkerConfigPath)
        $config = Get-Content -LiteralPath $hostConfigFile -Raw -ErrorAction Stop | ConvertFrom-Json
        $required = @(
            'worker_id', 'api_base_uri', 'key_id', 'shared_secret_file', 'executable_path',
            'executable_sha256', 'executable_signer_thumbprint', 'work_root'
        )
        foreach ($name in $required) {
            if ($config.PSObject.Properties.Name -notcontains $name -or [string]::IsNullOrWhiteSpace([string]$config.$name)) {
                throw 'invalid host config'
            }
        }
        if ($config.PSObject.Properties.Name -notcontains 'sources' -or @($config.sources).Count -eq 0) {
            throw 'invalid host config'
        }
        $apiBase = [Uri]::new([string]$config.api_base_uri)
        if (-not $apiBase.IsAbsoluteUri -or -not $apiBase.IsLoopback -or $apiBase.UserInfo) { throw 'invalid host config' }
        if ([string]$config.executable_sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'invalid host config' }
        if (-not (Test-Path -LiteralPath ([string]$config.executable_path) -PathType Leaf)) { throw 'invalid host config' }
        if (-not (Test-Path -LiteralPath ([string]$config.shared_secret_file) -PathType Leaf)) { throw 'invalid host config' }
        if ($config.backup_exclusion_acknowledged -ne $true) { throw 'invalid host config' }
        foreach ($source in @($config.sources)) {
            if (
                [string]::IsNullOrWhiteSpace([string]$source.source_id) -or
                -not (Test-Path -LiteralPath ([string]$source.root) -PathType Container) -or
                @($source.documents).Count -eq 0
            ) { throw 'invalid host config' }
            foreach ($document in @($source.documents)) {
                if (
                    [string]::IsNullOrWhiteSpace([string]$document.document_id) -or
                    [string]::IsNullOrWhiteSpace([string]$document.relative_path) -or
                    [IO.Path]::IsPathRooted([string]$document.relative_path) -or
                    [string]$document.relative_path -match '(^|[\\/])\.\.([\\/]|$)'
                ) { throw 'invalid host config' }
            }
            $hostSourceCount++
        }
        $hostConfigReady = $hostSourceCount -gt 0
    } catch {
        Add-Blocker 'HOST_WORKER_CONFIG_INVALID_OR_UNAVAILABLE'
    }
}

$successfulReports = 0
if (Test-Path -LiteralPath $reportRoot -PathType Container) {
    foreach ($file in Get-ChildItem -LiteralPath $reportRoot -File -Filter '*.json') {
        try {
            $report = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            if (
                $report.probe_passed -eq $true -and
                $report.cleanup_complete -eq $true -and
                $report.output_content_logged -eq $false -and
                [string]$report.error_code -eq ''
            ) { $successfulReports++ }
        } catch { }
    }
}
if ($successfulReports -lt 3) { Add-Blocker 'UENCRYPTOR2_SUCCESS_EVIDENCE_INSUFFICIENT' }
$remainingHostJobs = if (Test-Path -LiteralPath $jobRoot -PathType Container) {
    @(Get-ChildItem -LiteralPath $jobRoot -Force).Count
} else { 0 }
if ($remainingHostJobs -ne 0) { Add-Blocker 'WINDOWS_PLAINTEXT_JOB_REMAINS' }

if (-not (Test-Path -LiteralPath $overlayPath -PathType Leaf)) { Add-Blocker 'GENERATIONLESS_COMPOSE_OVERLAY_MISSING' }
$appImageId = Get-ImageId 'docmind-ragflow:windows-dev'
if ([string]::IsNullOrWhiteSpace($appImageId)) {
    Add-Blocker 'APP_IMAGE_MISSING'
} elseif ($appImageId -ne $expectedAppImageId) {
    Add-Blocker 'APP_IMAGE_DIGEST_UNAPPROVED'
}
$officeImageReady = -not [string]::IsNullOrWhiteSpace((Get-ImageId 'docmind-docling-office:2.115.0-legacy-doc-amd64'))
if (-not $officeImageReady) { Add-Blocker 'DOCLING_OFFICE_IMAGE_MISSING' }

$infraHealth = [ordered]@{}
foreach ($service in @('mysql', 'es01', 'redis', 'minio')) {
    $state = Get-ContainerHealth "docmind-windows-dev-$service-1"
    $infraHealth[$service] = $state
    if ($state -notin @('healthy', 'running')) { Add-Blocker 'DEVELOPMENT_INFRA_NOT_HEALTHY' }
}

$result = [ordered]@{
    schema_version = 1
    ready = $blockers.Count -eq 0
    mode = 'generationless-cloud-source-e2e'
    answer_generation_required = $false
    answer_credential_loaded = $false
    jina_credential_available = $jinaReady
    host_worker_secret_ready = $workerSecretReady
    host_worker_config_ready = $hostConfigReady
    registered_source_count = $hostSourceCount
    approved_decryption_reports = $successfulReports
    remaining_windows_plaintext_jobs = $remainingHostJobs
    app_image_approved = $appImageId -eq $expectedAppImageId
    office_parser_image_ready = $officeImageReady
    infrastructure = $infraHealth
    blockers = @($blockers)
}
$result | ConvertTo-Json -Depth 4
if ($blockers.Count -ne 0) { exit 2 }
