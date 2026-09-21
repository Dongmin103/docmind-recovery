[CmdletBinding()]
param(
    [string]$DockerCommand = 'docker',
    [string]$EnvironmentPath
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
    $jsonText = (& $DockerCommand compose --env-file $relativeEnvironment -f $relativeCompose --profile full config --format json 2>&1 | Out-String)
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
if ($config.services.PSObject.Properties.Name -contains 'openviking') {
    throw 'OpenViking must not be present in the Windows development stack.'
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
    if ($service.container_name) {
        throw "Fixed container_name found on service $($serviceProperty.Name)."
    }
    foreach ($port in @($service.ports)) {
        if ($port -and $port.host_ip -ne '127.0.0.1') {
            throw "Non-loopback published port found on service $($serviceProperty.Name): $($port | ConvertTo-Json -Compress)"
        }
    }
    foreach ($volume in @($service.volumes)) {
        if (-not $volume) { continue }
        foreach ($root in $forbiddenRoots) {
            if ([string]$volume.source -like "$root*") {
                throw "Forbidden source-root mount found on service $($serviceProperty.Name)."
            }
        }
    }
}

foreach ($volumeProperty in $config.volumes.PSObject.Properties) {
    if ($volumeProperty.Value.name -notlike 'docmind-windows-dev_*') {
        throw "Development volume escaped the isolated namespace: $($volumeProperty.Value.name)"
    }
}

$serialized = $config | ConvertTo-Json -Depth 100
if ($serialized -match '(?i)openviking') {
    throw 'OpenViking configuration remains in the resolved Windows development stack.'
}
if ($serialized -match '(?i)uencryptor') {
    throw 'The Docker stack must not invoke or configure uEncryptor.'
}
Write-Output 'Windows development Compose validation passed.'
Write-Output 'Verified: isolated project, no fixed container names, loopback-only published ports, no source-root mounts, and no OpenViking.'
