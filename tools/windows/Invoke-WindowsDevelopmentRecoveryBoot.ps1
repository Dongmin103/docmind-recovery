[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string]$PackagePath,
    [Parameter(Mandatory)] [string]$RecoveryKeyPath,
    [Parameter(Mandatory)] [string]$ReceiptPath,
    [Parameter(Mandatory)] [string]$MarkerPath,
    [Parameter(Mandatory)] [string]$EvidencePath,
    [string]$EnvironmentPath,
    [string]$DockerCommand = 'docker',
    [ValidateRange(1024, 65535)] [int]$MySqlPort = 23306,
    [ValidateRange(1024, 65535)] [int]$ElasticsearchPort = 29200,
    [ValidateRange(1024, 65535)] [int]$MinioPort = 29000,
    [ValidateRange(1024, 65535)] [int]$MinioConsolePort = 29001,
    [ValidateRange(30, 1800)] [int]$HealthTimeoutSeconds = 300,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$whatIfRequested = [bool]$WhatIfPreference
$WhatIfPreference = $false
. (Join-Path $PSScriptRoot 'WindowsDevelopmentRecovery.Common.ps1')

function Read-RecoveryBootEnvironment([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed -notmatch '^([A-Z][A-Z0-9_]*)=(.*)$') {
            throw 'Recovery boot environment contains an unsupported line.'
        }
        if ($values.ContainsKey($Matches[1])) {
            throw "Recovery boot environment contains a duplicate key: $($Matches[1])"
        }
        $values[$Matches[1]] = $Matches[2]
    }
    return $values
}

function Get-VerifiedRecoveryImageId {
    param(
        [Parameter(Mandatory)] [string]$ManifestValue,
        [Parameter(Mandatory)] [string]$Service
    )
    if ($ManifestValue -notmatch '^(?<reference>.+)@(?<imageId>sha256:[0-9a-f]{64})$') {
        throw "Recovery manifest has no exact image ID for $Service."
    }
    $reference = $Matches.reference
    $expectedImageId = $Matches.imageId
    $localId = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
        'image', 'inspect', $reference, '--format', '{{.Id}}'
    ) -Capture
    if ($localId -ne $expectedImageId) {
        throw "Local image does not match the authenticated recovery manifest for $Service."
    }
    return $expectedImageId
}

function Assert-RecoveryVolume {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$BackupId,
        [Parameter(Mandatory)] [string]$Service
    )
    $inspection = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
        'volume', 'inspect', $Name
    ) -Capture | ConvertFrom-Json
    $labels = $inspection[0].Labels
    if ([string]$labels.'com.docmind.recovery' -ne 'true' -or
        [string]$labels.'com.docmind.backup-id' -ne $BackupId -or
        [string]$labels.'com.docmind.service' -ne $Service) {
        throw "Recovery volume labels do not match the verified receipt: $Name"
    }
    $attached = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
        'ps', '--all', '--quiet', '--filter', "volume=$Name"
    ) -Capture
    if ($attached) {
        throw "Recovery volume is already attached to a container: $Name"
    }
}

$repoRoot = Get-RecoveryRepositoryRoot
if (-not $EnvironmentPath) { $EnvironmentPath = Join-Path $repoRoot '.local/docker/windows-dev.env' }
$resolvedEnvironment = [IO.Path]::GetFullPath($EnvironmentPath)
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
$localPrefix = $localRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $resolvedEnvironment.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-Path -LiteralPath $resolvedEnvironment -PathType Leaf)) {
    throw 'Recovery boot accepts only an existing ignored environment file below .local.'
}

$resolvedPackage = Assert-RecoveryPath -Path $PackagePath -Purpose 'Encrypted recovery package' -MustExist
$resolvedKey = Assert-RecoveryPath -Path $RecoveryKeyPath -Purpose 'Recovery key' -MustExist
$resolvedReceipt = Assert-RecoveryPath -Path $ReceiptPath -Purpose 'Recovery receipt' -MustExist
$resolvedMarker = Assert-RecoveryPath -Path $MarkerPath -Purpose 'Synthetic recovery marker receipt' -MustExist
$resolvedEvidence = Assert-RecoveryPath -Path $EvidencePath -Purpose 'Recovery boot evidence'
if (Test-Path -LiteralPath $resolvedEvidence) {
    throw "Refusing to overwrite recovery boot evidence: $resolvedEvidence"
}
$uniquePorts = @(@($MySqlPort, $ElasticsearchPort, $MinioPort, $MinioConsolePort) | Select-Object -Unique)
if ($uniquePorts.Count -ne 4) {
    throw 'Recovery boot ports must be distinct.'
}

$environment = Read-RecoveryBootEnvironment $resolvedEnvironment
foreach ($name in @('MYSQL_PASSWORD', 'ELASTIC_PASSWORD', 'MINIO_USER', 'MINIO_PASSWORD')) {
    if (-not $environment.ContainsKey($name) -or [string]$environment[$name] -notmatch '^[A-Za-z0-9]{12,128}$') {
        throw "Recovery boot requires a generated non-placeholder value for $name."
    }
}
if ($environment.DOCMIND_DEV_SECURITY_MODE -ne 'isolated-synthetic-only' -or
    $environment.DOCMIND_DEV_DATA_CLASS -ne 'synthetic-only') {
    throw 'Recovery boot is allowed only for the isolated synthetic-only development environment.'
}

$receipt = Get-Content -LiteralPath $resolvedReceipt -Raw | ConvertFrom-Json
$marker = Get-Content -LiteralPath $resolvedMarker -Raw | ConvertFrom-Json
if ($receipt.format -ne 'docmind-windows-dev-recovery-receipt/v1' -or
    [string]$receipt.backupId -notmatch '^[0-9a-f]{32}$') {
    throw 'Unsupported or invalid recovery receipt.'
}
if ($marker.format -ne 'docmind-recovery-marker/v1' -or
    $marker.composeProject -ne $script:RecoveryProject -or
    [string]$marker.markerId -notmatch '^[0-9a-f]{32}$' -or
    [string]$marker.sha256 -notmatch '^[0-9a-f]{64}$') {
    throw 'Synthetic marker receipt is invalid or belongs to another backup.'
}

$key = $null
$work = $null
$project = "docmind-recovery-boot-$(([string]$receipt.backupId).Substring(0, 12))"
$composeRelative = 'app/docker/docker-compose-windows-recovery-boot.yml'
$started = $false
try {
    $sidecarPath = "$resolvedPackage.manifest.json"
    if (-not (Test-Path -LiteralPath $sidecarPath -PathType Leaf)) {
        throw 'Recovery package sidecar manifest is required.'
    }
    $sidecar = Get-Content -LiteralPath $sidecarPath -Raw | ConvertFrom-Json
    if ($sidecar.backupId -ne $receipt.backupId -or
        $sidecar.packageSha256 -ne (Get-FileSha256 $resolvedPackage) -or
        $receipt.sourcePackageSha256 -ne $sidecar.packageSha256) {
        throw 'Package, sidecar, receipt, and marker identities do not agree.'
    }

    $key = Read-RecoveryKey $resolvedKey
    $work = New-RecoveryWorkingDirectory
    $plain = Join-Path $work 'payload.tar.gz'
    $payload = Join-Path $work 'payload'
    [IO.Directory]::CreateDirectory($payload) | Out-Null
    Unprotect-RecoveryFile -InputPath $resolvedPackage -OutputPath $plain -Key $key
    Assert-ArchiveEntriesSafe $plain
    & tar -xzf $plain -C $payload
    if ($LASTEXITCODE -ne 0) { throw 'Authenticated recovery archive extraction failed.' }
    $manifest = Read-RecoveryManifest $payload
    if ($manifest.backupId -ne $receipt.backupId -or $manifest.composeProject -ne $script:RecoveryProject) {
        throw 'Authenticated recovery manifest does not match the receipt.'
    }
    if ($manifest.PSObject.Properties.Name -notcontains 'syntheticMarker' -or
        [string]$manifest.syntheticMarker.markerId -ne [string]$marker.markerId -or
        [string]$manifest.syntheticMarker.sha256 -ne [string]$marker.sha256) {
        throw 'Authenticated recovery manifest does not contain the expected synthetic marker.'
    }
    foreach ($imageName in @('mysql', 'es01', 'minio')) {
        if ($manifest.images.PSObject.Properties.Name -notcontains $imageName) {
            throw "Authenticated recovery manifest is missing image identity: $imageName"
        }
    }

    $mysqlImage = Get-VerifiedRecoveryImageId -ManifestValue ([string]$manifest.images.mysql) -Service 'mysql'
    $esImage = Get-VerifiedRecoveryImageId -ManifestValue ([string]$manifest.images.es01) -Service 'elasticsearch'
    $minioImage = Get-VerifiedRecoveryImageId -ManifestValue ([string]$manifest.images.minio) -Service 'minio'

    $volumeNames = [ordered]@{
        mysql = [string]$receipt.volumes.mysql
        elasticsearch = [string]$receipt.volumes.elasticsearch
        minio = [string]$receipt.volumes.minio
    }
    foreach ($entry in $volumeNames.GetEnumerator()) {
        if (-not $entry.Value.StartsWith("docmind-windows-dev-recovery-$($receipt.backupId.Substring(0, 12))_", [StringComparison]::Ordinal)) {
            throw "Recovery receipt contains an unexpected volume name for $($entry.Key)."
        }
        Assert-RecoveryVolume -Name $entry.Value -BackupId $receipt.backupId -Service $entry.Key
    }

    $existingProjectContainers = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
        'ps', '--all', '--quiet', '--filter', "label=com.docker.compose.project=$project"
    ) -Capture
    if ($existingProjectContainers) {
        throw "Recovery boot project already exists; run the explicit cleanup first: $project"
    }

    $runtimeEnv = Join-Path $work 'recovery-boot.env'
    $runtimeLines = @(
        "RECOVERY_COMPOSE_PROJECT=$project",
        "RECOVERY_BACKUP_ID=$($receipt.backupId)",
        "RECOVERY_MYSQL_IMAGE=$mysqlImage",
        "RECOVERY_ELASTICSEARCH_IMAGE=$esImage",
        "RECOVERY_MINIO_IMAGE=$minioImage",
        "RECOVERY_MYSQL_VOLUME=$($volumeNames.mysql)",
        "RECOVERY_ES_VOLUME=$($volumeNames.elasticsearch)",
        "RECOVERY_MINIO_VOLUME=$($volumeNames.minio)",
        "RECOVERY_MYSQL_PORT=$MySqlPort",
        "RECOVERY_ES_PORT=$ElasticsearchPort",
        "RECOVERY_MINIO_PORT=$MinioPort",
        "RECOVERY_MINIO_CONSOLE_PORT=$MinioConsolePort",
        "MYSQL_PASSWORD=$($environment.MYSQL_PASSWORD)",
        "ELASTIC_PASSWORD=$($environment.ELASTIC_PASSWORD)",
        "MINIO_USER=$($environment.MINIO_USER)",
        "MINIO_PASSWORD=$($environment.MINIO_PASSWORD)",
        "TZ=$($environment.TZ)"
    )
    [IO.File]::WriteAllLines($runtimeEnv, $runtimeLines, [Text.UTF8Encoding]::new($false))
    $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $relativeRuntimeEnv = $runtimeEnv.Substring($repoPrefix.Length).Replace('\', '/')
    $composePrefix = @('compose', '-p', $project, '--env-file', $relativeRuntimeEnv, '-f', $composeRelative)

    Push-Location $repoRoot
    try {
        $config = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments ($composePrefix + @(
            'config', '--format', 'json'
        )) -Capture | ConvertFrom-Json
        $expectedServices = [ordered]@{
            mysql = [ordered]@{ image = $mysqlImage; volume = 'mysql_recovery' }
            elasticsearch = [ordered]@{ image = $esImage; volume = 'es_recovery' }
            minio = [ordered]@{ image = $minioImage; volume = 'minio_recovery' }
        }
        if ($config.name -ne $project -or @($config.services.PSObject.Properties).Count -ne 3) {
            throw 'Resolved recovery Compose escaped the isolated three-service project.'
        }
        foreach ($serviceName in $expectedServices.Keys) {
            if ($config.services.PSObject.Properties.Name -notcontains $serviceName) {
                throw "Resolved recovery Compose is missing the expected service: $serviceName"
            }
            $service = $config.services.$serviceName
            if ([string]$service.image -ne [string]$expectedServices[$serviceName].image -or
                [string]$service.pull_policy -ne 'never' -or
                [string]$service.labels.'com.docmind.recovery-boot' -ne 'true' -or
                [string]$service.labels.'com.docmind.backup-id' -ne [string]$receipt.backupId) {
                throw "Resolved recovery service identity is unsafe: $serviceName"
            }
            $mounts = @($service.volumes)
            if ($mounts.Count -ne 1 -or $mounts[0].type -ne 'volume' -or
                [string]$mounts[0].source -ne [string]$expectedServices[$serviceName].volume) {
                throw "Resolved recovery service has an unexpected mount: $serviceName"
            }
            foreach ($port in @($service.ports)) {
                if ($port.host_ip -ne '127.0.0.1') {
                    throw "Recovery service has a non-loopback port: $serviceName"
                }
            }
        }
        if (@($config.networks.PSObject.Properties).Count -ne 1) {
            throw 'Resolved recovery Compose must contain exactly one network.'
        }
        $resolvedNetwork = @($config.networks.PSObject.Properties)[0].Value
        if (-not [bool]$resolvedNetwork.internal -or
            [string]$resolvedNetwork.labels.'com.docmind.recovery-boot' -ne 'true' -or
            [string]$resolvedNetwork.labels.'com.docmind.backup-id' -ne [string]$receipt.backupId) {
            throw 'Resolved recovery Compose network is not isolated and recovery-bound.'
        }
        $expectedVolumeNames = [ordered]@{
            mysql_recovery = $volumeNames.mysql
            es_recovery = $volumeNames.elasticsearch
            minio_recovery = $volumeNames.minio
        }
        if (@($config.volumes.PSObject.Properties).Count -ne 3) {
            throw 'Resolved recovery Compose must contain exactly three external volumes.'
        }
        foreach ($volumeKey in $expectedVolumeNames.Keys) {
            if ($config.volumes.PSObject.Properties.Name -notcontains $volumeKey -or
                -not [bool]$config.volumes.$volumeKey.external -or
                [string]$config.volumes.$volumeKey.name -ne [string]$expectedVolumeNames[$volumeKey]) {
                throw "Resolved recovery Compose volume does not match the receipt: $volumeKey"
            }
        }

        Write-Output "Verified recovery boot project: $project"
        Write-Output 'Resources are preserved after success or failure; cleanup is never automatic.'
        if ($ValidateOnly -or $whatIfRequested) {
            Write-Output 'Recovery boot input validation passed; no container or volume was started or attached.'
            return
        }
        if (-not $PSCmdlet.ShouldProcess($project, 'start isolated recovery services and verify health plus synthetic markers')) {
            return
        }
        Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments ($composePrefix + @('up', '-d'))
        $started = $true

        $containerIds = [ordered]@{}
        foreach ($service in @('mysql', 'elasticsearch', 'minio')) {
            $containerIds[$service] = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments (
                $composePrefix + @('ps', '--quiet', $service)
            ) -Capture
            if (-not $containerIds[$service]) { throw "Recovery container was not created: $service" }
        }

        $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
        do {
            $allHealthy = $true
            foreach ($entry in $containerIds.GetEnumerator()) {
                $inspection = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
                    'inspect', $entry.Value
                ) -Capture | ConvertFrom-Json
                $status = [string]$inspection[0].State.Health.Status
                if ($status -eq 'unhealthy') { throw "Recovery service became unhealthy: $($entry.Key)" }
                if ($status -ne 'healthy') { $allHealthy = $false }
            }
            if (-not $allHealthy) { Start-Sleep -Seconds 2 }
        } while (-not $allHealthy -and [DateTime]::UtcNow -lt $deadline)
        if (-not $allHealthy) { throw 'Recovery services did not become healthy before the timeout.' }

        $markerId = [string]$marker.markerId
        $markerSha = [string]$marker.sha256
        $markerEnvironment = [ordered]@{ EXPECTED_ID = $markerId; EXPECTED_SHA = $markerSha }
        Invoke-RecoveryDockerScript `
            -DockerCommand $DockerCommand `
            -ContainerId $containerIds.mysql `
            -Environment $markerEnvironment `
            -Script @'
actual=$(MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -Nse "SELECT COUNT(*) FROM docmind_recovery_probe.markers WHERE marker_id = '$EXPECTED_ID' AND marker_sha256 = '$EXPECTED_SHA';")
test "$actual" = 1
'@
        Invoke-RecoveryDockerScript `
            -DockerCommand $DockerCommand `
            -ContainerId $containerIds.elasticsearch `
            -Environment $markerEnvironment `
            -Script @'
body=$(curl -fsS -u elastic:"$ELASTIC_PASSWORD" "http://127.0.0.1:9200/docmind-recovery-probe/_doc/$EXPECTED_ID")
expected_found='"found":true'
expected_hash="\"marker_sha256\":\"$EXPECTED_SHA\""
case "$body" in *"$expected_found"*) : ;; *) exit 1 ;; esac
case "$body" in *"$expected_hash"*) : ;; *) exit 1 ;; esac
'@
        Invoke-RecoveryDockerScript `
            -DockerCommand $DockerCommand `
            -ContainerId $containerIds.minio `
            -Environment $markerEnvironment `
            -Script @'
mc alias set recovery http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
actual_sha=$(mc cat "recovery/docmind-recovery-probe/$EXPECTED_ID" | sha256sum)
case "$actual_sha" in "$EXPECTED_SHA "*) : ;; *) exit 1 ;; esac
'@

        $networkText = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
            'network', 'ls', '--quiet', '--filter', "label=com.docker.compose.project=$project"
        ) -Capture
        $networkNames = @($networkText -split "`r?`n" | Where-Object { $_ })
        if ($networkNames.Count -ne 1) { throw 'Recovery project did not create exactly one isolated network.' }
        $networkInspection = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
            'network', 'inspect', $networkNames[0]
        ) -Capture | ConvertFrom-Json
        if (-not [bool]$networkInspection[0].Internal -or
            [string]$networkInspection[0].Labels.'com.docmind.backup-id' -ne $receipt.backupId) {
            throw 'Recovery network is not internal or does not match the backup identity.'
        }

        [IO.Directory]::CreateDirectory((Split-Path -Parent $resolvedEvidence)) | Out-Null
        $evidence = [ordered]@{
            format = 'docmind-recovery-boot-evidence/v1'
            backupId = $receipt.backupId
            project = $project
            verifiedUtc = [DateTime]::UtcNow.ToString('o')
            markerId = $markerId
            markerSha256 = $markerSha
            containers = $containerIds
            networkId = [string]$networkNames[0]
            volumes = $volumeNames
            ports = [ordered]@{
                mysql = $MySqlPort
                elasticsearch = $ElasticsearchPort
                minio = $MinioPort
                minioConsole = $MinioConsolePort
            }
            cleanup = 'Explicitly run Stop-WindowsDevelopmentRecoveryBoot.ps1; restored volumes are never deleted.'
        }
        [IO.File]::WriteAllText(
            $resolvedEvidence,
            ($evidence | ConvertTo-Json -Depth 8),
            [Text.UTF8Encoding]::new($false)
        )
        Write-Output "Recovery boot verification passed; evidence: $resolvedEvidence"
        Write-Output 'Verified three healthy services and the expected synthetic marker in MySQL, Elasticsearch, and MinIO.'
        Write-Output 'Containers, network, and restored volumes remain intact for explicit operator cleanup.'
    } finally {
        Pop-Location
    }
} catch {
    if ($started) {
        Write-Warning "Recovery boot verification failed. Isolated resources were preserved for explicit cleanup: $project"
    }
    throw
} finally {
    try {
        if ($work) { Remove-RecoveryWorkingDirectory -Path $work }
    } finally {
        if ($null -ne $key) { [Array]::Clear($key, 0, $key.Length) }
    }
}
