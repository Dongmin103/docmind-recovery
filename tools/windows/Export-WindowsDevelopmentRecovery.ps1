[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string]$OutputPath,
    [Parameter(Mandatory)] [string]$RecoveryKeyPath,
    [string]$EnvironmentPath,
    [string]$DockerCommand = 'docker',
    [ValidateSet('Native', 'Wsl')] [string]$DockerHostPathStyle = 'Native',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'WindowsDevelopmentRecovery.Common.ps1')

$repoRoot = Get-RecoveryRepositoryRoot
if (-not $EnvironmentPath) { $EnvironmentPath = Join-Path $repoRoot '.local/docker/windows-dev.env' }
$resolvedEnvironment = [IO.Path]::GetFullPath($EnvironmentPath)
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
if (-not $resolvedEnvironment.StartsWith($localRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Recovery export accepts only an ignored development environment file below .local.'
}
if (-not (Test-Path -LiteralPath $resolvedEnvironment -PathType Leaf)) {
    throw "Missing development environment: $resolvedEnvironment"
}
$resolvedOutput = Assert-RecoveryPath -Path $OutputPath -Purpose 'Encrypted recovery output'
$resolvedKey = Assert-RecoveryPath -Path $RecoveryKeyPath -Purpose 'Recovery key' -MustExist
$sidecarPath = "$resolvedOutput.manifest.json"
if ((Test-Path -LiteralPath $resolvedOutput) -or (Test-Path -LiteralPath $sidecarPath)) {
    throw 'Refusing to overwrite an existing recovery package or sidecar manifest.'
}

$plan = @(
    "Compose project: $script:RecoveryProject",
    'Source volumes: mysql_data, es_data, minio_data',
    'Published interfaces: validated loopback-only by Test-WindowsDevelopmentCompose.ps1',
    'Excluded: redis_data (cache, leases, locks, and queues must be rebuilt)',
    "Encrypted output: $resolvedOutput",
    'Original source roots and uEncryptor2: never accessed'
)
if ($DryRun) {
    $plan | ForEach-Object { Write-Output $_ }
    Write-Output 'Dry run completed without invoking Docker or reading service data.'
    return
}

if (-not $PSCmdlet.ShouldProcess($script:RecoveryProject, 'stop three development services briefly and export encrypted recovery package')) {
    return
}

& (Join-Path $PSScriptRoot 'Test-WindowsDevelopmentCompose.ps1') -DockerCommand $DockerCommand -EnvironmentPath $resolvedEnvironment
$key = Read-RecoveryKey $resolvedKey
[IO.Directory]::CreateDirectory((Split-Path -Parent $resolvedOutput)) | Out-Null
$work = New-RecoveryWorkingDirectory
$payloadRoot = Join-Path $work 'payload'
[IO.Directory]::CreateDirectory($payloadRoot) | Out-Null
$dockerPayloadRoot = Convert-RecoveryDockerBindPath -Path $payloadRoot -Style $DockerHostPathStyle
$plainArchive = Join-Path $work 'payload.tar.gz'
$services = @('mysql', 'es01', 'minio')
$relativeEnvironment = $resolvedEnvironment.Substring(
    $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar).Length + 1
).Replace('\', '/')
$composePrefix = @(
    'compose', '-p', $script:RecoveryProject,
    '--env-file', $relativeEnvironment,
    '-f', 'app/docker/docker-compose-windows-dev.yml',
    '--profile', 'infra'
)
$running = @()
$stopped = $false
Push-Location $repoRoot
try {
    $runningText = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments ($composePrefix + @('ps', '--services', '--filter', 'status=running')) -Capture
    $running = @($runningText -split "`r?`n" | Where-Object { $_ })
    foreach ($service in $services) {
        if ($running -notcontains $service) {
            throw "Required development service is not running: $service"
        }
    }

    $containerImages = [ordered]@{}
    foreach ($service in $services) {
        $containerId = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments ($composePrefix + @('ps', '-q', $service)) -Capture
        if (-not $containerId) { throw "Cannot resolve development container for $service" }
        $containerImages[$service] = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
            'inspect', '--format', '{{.Config.Image}}@{{.Image}}', $containerId
        ) -Capture
    }

    # Pull the archive helper before entering the cold-snapshot window.
    Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @('pull', $script:RecoveryHelperImage)

    Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments ($composePrefix + @('stop', '--timeout', '60') + $services)
    $stopped = $true

    $volumeMap = [ordered]@{
        'mysql-data.tgz' = [ordered]@{ service = 'mysql'; volume = 'docmind-windows-dev_mysql_data' }
        'elasticsearch-data.tgz' = [ordered]@{ service = 'elasticsearch'; volume = 'docmind-windows-dev_es_data' }
        'minio-data.tgz' = [ordered]@{ service = 'minio'; volume = 'docmind-windows-dev_minio_data' }
    }
    foreach ($entry in $volumeMap.GetEnumerator()) {
        $volumeInspection = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
            'volume', 'inspect', $entry.Value.volume
        ) -Capture | ConvertFrom-Json
        if ([string]$volumeInspection[0].Labels.'com.docker.compose.project' -ne $script:RecoveryProject) {
            throw "Volume escaped the fixed development project: $($entry.Value.volume)"
        }
        Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
            'run', '--rm', '--network', 'none',
            '--mount', "type=volume,src=$($entry.Value.volume),dst=/source,readonly",
            '--mount', "type=bind,src=$dockerPayloadRoot,dst=/backup",
            $script:RecoveryHelperImage,
            'tar', '-C', '/source', '-czf', "/backup/$($entry.Key)", '.'
        )
    }

    $payloads = foreach ($entry in $volumeMap.GetEnumerator()) {
        $path = Join-Path $payloadRoot $entry.Key
        [ordered]@{
            service = $entry.Value.service
            file = $entry.Key
            sha256 = Get-FileSha256 $path
            bytes = (Get-Item -LiteralPath $path).Length
            sourceVolume = $entry.Value.volume
        }
    }
    $backupId = [Guid]::NewGuid().ToString('N')
    $manifest = [ordered]@{
        format = $script:RecoveryFormat
        backupId = $backupId
        createdUtc = [DateTime]::UtcNow.ToString('o')
        composeProject = $script:RecoveryProject
        consistency = 'cold-volume-snapshot'
        encryption = 'AES-256-CBC + HMAC-SHA256; independent 256-bit keys'
        images = $containerImages
        payloads = @($payloads)
        excluded = @(
            [ordered]@{
                service = 'valkey'
                volume = 'docmind-windows-dev_redis_data'
                reason = 'Ephemeral cache, leases, locks, and queues are rebuilt to prevent stale work replay.'
            }
        )
    }
    [IO.File]::WriteAllText(
        (Join-Path $payloadRoot 'manifest.json'),
        ($manifest | ConvertTo-Json -Depth 10),
        [Text.UTF8Encoding]::new($false)
    )
    Push-Location $payloadRoot
    try {
        & tar -czf $plainArchive .
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the temporary recovery archive.' }
    } finally {
        Pop-Location
    }
    Protect-RecoveryFile -InputPath $plainArchive -OutputPath $resolvedOutput -Key $key
    $sidecar = [ordered]@{
        format = $script:RecoveryFormat
        backupId = $backupId
        createdUtc = $manifest.createdUtc
        composeProject = $script:RecoveryProject
        packageSha256 = Get-FileSha256 $resolvedOutput
        packageBytes = (Get-Item -LiteralPath $resolvedOutput).Length
        services = @('mysql', 'elasticsearch', 'minio')
        excluded = @('valkey')
    }
    [IO.File]::WriteAllText(
        $sidecarPath,
        ($sidecar | ConvertTo-Json -Depth 5),
        [Text.UTF8Encoding]::new($false)
    )
} finally {
    if ($stopped) {
        $toStart = @($services | Where-Object { $running -contains $_ })
        if ($toStart.Count -gt 0) {
            Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments ($composePrefix + @('up', '-d', '--no-recreate') + $toStart)
        }
    }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    [Array]::Clear($key, 0, $key.Length)
    Pop-Location
}

Write-Output "Encrypted recovery package created: $resolvedOutput"
Write-Output "Public checksum manifest created: $sidecarPath"
Write-Output 'No credentials or document contents were written to the console.'
