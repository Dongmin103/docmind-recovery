[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)] [string]$EvidencePath,
    [string]$DockerCommand = 'docker'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'WindowsDevelopmentRecovery.Common.ps1')

$resolvedEvidence = Assert-RecoveryPath -Path $EvidencePath -Purpose 'Recovery boot evidence' -MustExist
$evidence = Get-Content -LiteralPath $resolvedEvidence -Raw | ConvertFrom-Json
if ($evidence.format -ne 'docmind-recovery-boot-evidence/v1' -or
    [string]$evidence.backupId -notmatch '^[0-9a-f]{32}$' -or
    [string]$evidence.project -ne "docmind-recovery-boot-$(([string]$evidence.backupId).Substring(0, 12))") {
    throw 'Invalid recovery boot evidence.'
}

$containerIds = @($evidence.containers.PSObject.Properties | ForEach-Object { [string]$_.Value })
if ($containerIds.Count -ne 3 -or @($containerIds | Select-Object -Unique).Count -ne 3) {
    throw 'Recovery boot evidence must identify exactly three containers.'
}
foreach ($containerId in $containerIds) {
    $inspectionText = (& $DockerCommand inspect $containerId 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $inspectionText) {
        throw "Recovery container is missing; refusing partial implicit cleanup: $containerId"
    }
    $inspection = $inspectionText | ConvertFrom-Json
    $labels = $inspection[0].Config.Labels
    if ([string]$labels.'com.docmind.recovery-boot' -ne 'true' -or
        [string]$labels.'com.docmind.backup-id' -ne $evidence.backupId -or
        [string]$labels.'com.docker.compose.project' -ne $evidence.project) {
        throw "Container labels do not match recovery evidence: $containerId"
    }
}

$networkInspection = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
    'network', 'inspect', [string]$evidence.networkId
) -Capture | ConvertFrom-Json
if ([string]$networkInspection[0].Labels.'com.docmind.recovery-boot' -ne 'true' -or
    [string]$networkInspection[0].Labels.'com.docmind.backup-id' -ne $evidence.backupId -or
    [string]$networkInspection[0].Labels.'com.docker.compose.project' -ne $evidence.project) {
    throw 'Network labels do not match recovery evidence.'
}

Write-Output "Explicit cleanup target: $($evidence.project)"
Write-Output 'This removes only the three verified test containers and their isolated network.'
Write-Output 'Restored volumes and evidence are retained.'
if (-not $PSCmdlet.ShouldProcess($evidence.project, 'stop and remove verified recovery boot containers and network')) {
    return
}

Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments (@('stop', '--timeout', '60') + $containerIds)
Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments (@('rm') + $containerIds)
Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
    'network', 'rm', [string]$evidence.networkId
)

foreach ($volumeProperty in $evidence.volumes.PSObject.Properties) {
    $inspection = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
        'volume', 'inspect', [string]$volumeProperty.Value
    ) -Capture | ConvertFrom-Json
    if ([string]$inspection[0].Labels.'com.docmind.backup-id' -ne $evidence.backupId) {
        throw "Retained recovery volume identity changed: $($volumeProperty.Value)"
    }
}
Write-Output 'Explicit recovery boot cleanup completed. All restored volumes and evidence remain available.'
