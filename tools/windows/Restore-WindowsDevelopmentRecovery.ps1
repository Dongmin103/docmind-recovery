[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string]$InputPath,
    [Parameter(Mandatory)] [string]$RecoveryKeyPath,
    [Parameter(Mandatory)] [string]$ReceiptPath,
    [string]$DockerCommand = 'docker',
    [ValidateSet('Native', 'Wsl')] [string]$DockerHostPathStyle = 'Native',
    [switch]$KeepFailedVolumes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'WindowsDevelopmentRecovery.Common.ps1')

$resolvedInput = Assert-RecoveryPath -Path $InputPath -Purpose 'Encrypted recovery package' -MustExist
$resolvedKey = Assert-RecoveryPath -Path $RecoveryKeyPath -Purpose 'Recovery key' -MustExist
$resolvedReceipt = Assert-RecoveryPath -Path $ReceiptPath -Purpose 'Recovery receipt'
if (Test-Path -LiteralPath $resolvedReceipt) {
    throw "Refusing to overwrite recovery receipt: $resolvedReceipt"
}
$sidecarPath = "$resolvedInput.manifest.json"
if (Test-Path -LiteralPath $sidecarPath -PathType Leaf) {
    $sidecar = Get-Content -LiteralPath $sidecarPath -Raw | ConvertFrom-Json
    if ($sidecar.composeProject -ne $script:RecoveryProject -or
        $sidecar.packageSha256 -ne (Get-FileSha256 $resolvedInput)) {
        throw 'Encrypted package checksum or fixed project identity does not match its sidecar manifest.'
    }
}

if ($DryRun) {
    Write-Output "Package: $resolvedInput"
    Write-Output 'Restore target: new, disconnected Docker volumes under docmind-windows-dev-recovery-*'
    Write-Output 'Existing development volumes are never removed, mounted, or overwritten.'
    Write-Output 'Original source roots, published ports, services, and uEncryptor2 are not accessed.'
    return
}
if (-not $PSCmdlet.ShouldProcess($resolvedInput, 'decrypt and restore into new disconnected Docker volumes')) {
    return
}

$key = Read-RecoveryKey $resolvedKey
$work = New-RecoveryWorkingDirectory
$plainArchive = Join-Path $work 'payload.tar.gz'
$payloadRoot = Join-Path $work 'payload'
[IO.Directory]::CreateDirectory($payloadRoot) | Out-Null
$dockerPayloadRoot = Convert-RecoveryDockerBindPath -Path $payloadRoot -Style $DockerHostPathStyle
$createdVolumes = [Collections.Generic.List[string]]::new()
$backupId = $null
$completed = $false
try {
    Unprotect-RecoveryFile -InputPath $resolvedInput -OutputPath $plainArchive -Key $key
    Assert-ArchiveEntriesSafe $plainArchive
    & tar -xzf $plainArchive -C $payloadRoot
    if ($LASTEXITCODE -ne 0) { throw 'Failed to extract the authenticated recovery package.' }
    $manifest = Read-RecoveryManifest $payloadRoot
    $backupId = [string]$manifest.backupId
    $shortId = ([string]$manifest.backupId).Substring(0, 12)
    $restorePrefix = "docmind-windows-dev-recovery-$shortId"
    $volumeNames = [ordered]@{
        mysql = "${restorePrefix}_mysql_data"
        elasticsearch = "${restorePrefix}_es_data"
        minio = "${restorePrefix}_minio_data"
    }

    foreach ($payload in $manifest.payloads) {
        $volumeName = [string]$volumeNames.($payload.service)
        $existing = (& $DockerCommand volume ls --quiet --filter "name=^$([Regex]::Escape($volumeName))$" 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect Docker recovery volumes.' }
        if ($existing) { throw "Refusing to overwrite existing Docker volume: $volumeName" }
    }
    foreach ($payload in $manifest.payloads) {
        $volumeName = [string]$volumeNames.($payload.service)
        Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
            'volume', 'create',
            '--label', 'com.docmind.recovery=true',
            '--label', "com.docmind.backup-id=$($manifest.backupId)",
            '--label', "com.docmind.service=$($payload.service)",
            $volumeName
        ) | Out-Null
        $createdVolumes.Add($volumeName)
        Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
            'run', '--rm', '--network', 'none',
            '--mount', "type=volume,src=$volumeName,dst=/restore",
            '--mount', "type=bind,src=$dockerPayloadRoot,dst=/backup,readonly",
            $script:RecoveryHelperImage,
            'tar', '-C', '/restore', '-xzf', "/backup/$($payload.file)"
        )
    }

    [IO.Directory]::CreateDirectory((Split-Path -Parent $resolvedReceipt)) | Out-Null
    $receipt = [ordered]@{
        format = 'docmind-windows-dev-recovery-receipt/v1'
        backupId = $manifest.backupId
        restoredUtc = [DateTime]::UtcNow.ToString('o')
        sourcePackageSha256 = Get-FileSha256 $resolvedInput
        volumes = $volumeNames
        connectedToServices = $false
        publishedPorts = $false
        verification = 'Run Test-WindowsDevelopmentRecovery.ps1 with this receipt before using the volumes.'
    }
    [IO.File]::WriteAllText(
        $resolvedReceipt,
        ($receipt | ConvertTo-Json -Depth 5),
        [Text.UTF8Encoding]::new($false)
    )
    $completed = $true
} finally {
    if (-not $completed -and -not $KeepFailedVolumes) {
        foreach ($volumeName in $createdVolumes) {
            $inspectionText = (& $DockerCommand volume inspect $volumeName 2>$null | Out-String).Trim()
            $inspection = if ($LASTEXITCODE -eq 0 -and $inspectionText) { $inspectionText | ConvertFrom-Json } else { $null }
            if ($inspection -and [string]$inspection[0].Labels.'com.docmind.backup-id' -eq $backupId) {
                & $DockerCommand volume rm $volumeName *> $null
            }
        }
    }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    [Array]::Clear($key, 0, $key.Length)
}

Write-Output "Recovery volumes created without replacing development data. Receipt: $resolvedReceipt"
Write-Output 'The restored volumes are disconnected and publish no ports; run the recovery test before attaching them.'
