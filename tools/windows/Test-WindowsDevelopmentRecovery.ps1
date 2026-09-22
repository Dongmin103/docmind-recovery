[CmdletBinding()]
param(
    [string]$PackagePath,
    [string]$RecoveryKeyPath,
    [string]$ReceiptPath,
    [string]$DockerCommand = 'docker',
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'WindowsDevelopmentRecovery.Common.ps1')

if ($SelfTest) {
    $work = New-RecoveryWorkingDirectory
    $key = $null
    try {
        $fixture = Join-Path $work 'fixture'
        [IO.Directory]::CreateDirectory($fixture) | Out-Null
        foreach ($name in @('mysql-data.tgz', 'elasticsearch-data.tgz', 'minio-data.tgz')) {
            [IO.File]::WriteAllText((Join-Path $fixture $name), "synthetic-$name", [Text.UTF8Encoding]::new($false))
        }
        $payloads = foreach ($item in @(
            [ordered]@{ service = 'mysql'; file = 'mysql-data.tgz' },
            [ordered]@{ service = 'elasticsearch'; file = 'elasticsearch-data.tgz' },
            [ordered]@{ service = 'minio'; file = 'minio-data.tgz' }
        )) {
            $fixturePath = Join-Path $fixture $item.file
            [ordered]@{
                service = $item.service
                file = $item.file
                sha256 = Get-FileSha256 $fixturePath
                bytes = (Get-Item -LiteralPath $fixturePath).Length
            }
        }
        $manifest = [ordered]@{
            format = $script:RecoveryFormat
            backupId = [Guid]::NewGuid().ToString('N')
            createdUtc = [DateTime]::UtcNow.ToString('o')
            composeProject = $script:RecoveryProject
            syntheticMarker = [ordered]@{
                markerId = '0123456789abcdef0123456789abcdef'
                sha256 = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
            }
            payloads = $payloads
        }
        [IO.File]::WriteAllText((Join-Path $fixture 'manifest.json'), ($manifest | ConvertTo-Json -Depth 5))
        $plain = Join-Path $work 'fixture.tar.gz'
        Push-Location $fixture
        try { & tar -czf $plain . } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic fixture packaging failed.' }
        $key = [byte[]]::new(64)
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $generator.GetBytes($key)
        } finally {
            $generator.Dispose()
        }
        $encrypted = Join-Path $work 'fixture.dmrecovery'
        $decrypted = Join-Path $work 'decrypted.tar.gz'
        Protect-RecoveryFile -InputPath $plain -OutputPath $encrypted -Key $key
        Unprotect-RecoveryFile -InputPath $encrypted -OutputPath $decrypted -Key $key
        if ((Get-FileSha256 $plain) -ne (Get-FileSha256 $decrypted)) {
            throw 'Synthetic encrypted recovery round trip changed the archive.'
        }
        $bytes = [IO.File]::ReadAllBytes($encrypted)
        $bytes[[Math]::Floor($bytes.Length / 2)] = $bytes[[Math]::Floor($bytes.Length / 2)] -bxor 1
        $tampered = Join-Path $work 'tampered.dmrecovery'
        [IO.File]::WriteAllBytes($tampered, $bytes)
        $tamperRejected = $false
        try {
            Unprotect-RecoveryFile -InputPath $tampered -OutputPath (Join-Path $work 'must-not-exist.tar.gz') -Key $key
        } catch {
            $tamperRejected = $true
        }
        if (-not $tamperRejected) { throw 'Tampered recovery package was not rejected.' }
        $unpacked = Join-Path $work 'unpacked'
        [IO.Directory]::CreateDirectory($unpacked) | Out-Null
        Assert-ArchiveEntriesSafe $decrypted
        & tar -xzf $decrypted -C $unpacked
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic recovery extraction failed.' }
        $verifiedManifest = Read-RecoveryManifest $unpacked
        if ([string]$verifiedManifest.syntheticMarker.markerId -ne [string]$manifest.syntheticMarker.markerId -or
            [string]$verifiedManifest.syntheticMarker.sha256 -ne [string]$manifest.syntheticMarker.sha256) {
            throw 'Authenticated synthetic recovery marker changed during the encrypted round trip.'
        }
        Write-Output 'Recovery self-test passed: encryption round trip, HMAC tamper rejection, safe archive paths, payload checksums, and authenticated marker binding.'
    } finally {
        if ($null -ne $key) { [Array]::Clear($key, 0, $key.Length) }
        Remove-RecoveryWorkingDirectory -Path $work
    }
    return
}

if (-not $PackagePath -or -not $RecoveryKeyPath) {
    throw 'Supply -PackagePath and -RecoveryKeyPath, or run -SelfTest.'
}
$resolvedPackage = Assert-RecoveryPath -Path $PackagePath -Purpose 'Encrypted recovery package' -MustExist
$resolvedKey = Assert-RecoveryPath -Path $RecoveryKeyPath -Purpose 'Recovery key' -MustExist
$sidecarPath = "$resolvedPackage.manifest.json"
if (-not (Test-Path -LiteralPath $sidecarPath -PathType Leaf)) {
    throw 'Recovery package sidecar manifest is required for verification.'
}
$sidecar = Get-Content -LiteralPath $sidecarPath -Raw | ConvertFrom-Json
if ($sidecar.composeProject -ne $script:RecoveryProject -or
    $sidecar.packageSha256 -ne (Get-FileSha256 $resolvedPackage)) {
    throw 'Recovery package checksum or project identity verification failed.'
}

$key = Read-RecoveryKey $resolvedKey
$work = New-RecoveryWorkingDirectory
try {
    $plain = Join-Path $work 'payload.tar.gz'
    $payloadRoot = Join-Path $work 'payload'
    [IO.Directory]::CreateDirectory($payloadRoot) | Out-Null
    Unprotect-RecoveryFile -InputPath $resolvedPackage -OutputPath $plain -Key $key
    Assert-ArchiveEntriesSafe $plain
    & tar -xzf $plain -C $payloadRoot
    if ($LASTEXITCODE -ne 0) { throw 'Authenticated recovery archive extraction failed.' }
    $manifest = Read-RecoveryManifest $payloadRoot
    if ($manifest.backupId -ne $sidecar.backupId) {
        throw 'Encrypted and public recovery manifests disagree on backup identity.'
    }

    if ($ReceiptPath) {
        $resolvedReceipt = Assert-RecoveryPath -Path $ReceiptPath -Purpose 'Recovery receipt' -MustExist
        $receipt = Get-Content -LiteralPath $resolvedReceipt -Raw | ConvertFrom-Json
        if ($receipt.backupId -ne $manifest.backupId -or
            $receipt.sourcePackageSha256 -ne (Get-FileSha256 $resolvedPackage)) {
            throw 'Recovery receipt does not match the verified package.'
        }
        foreach ($property in $receipt.volumes.PSObject.Properties) {
            $inspection = Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
                'volume', 'inspect', [string]$property.Value
            ) -Capture | ConvertFrom-Json
            if ([string]$inspection[0].Labels.'com.docmind.backup-id' -ne $manifest.backupId) {
                throw "Restored volume label does not match backup identity: $($property.Value)"
            }
            $markerCommand = switch ($property.Name) {
                'mysql' { 'test -d /volume/mysql && test -f /volume/ibdata1' }
                'elasticsearch' { 'test -f /volume/nodes && test -d /volume/_state' }
                'minio' { 'test -d /volume/.minio.sys || find /volume -type f -print -quit | grep -q .' }
                default { throw "Unexpected service in recovery receipt: $($property.Name)" }
            }
            Invoke-RecoveryDocker -DockerCommand $DockerCommand -Arguments @(
                'run', '--rm', '--network', 'none',
                '--mount', "type=volume,src=$([string]$property.Value),dst=/volume,readonly",
                $script:RecoveryHelperImage,
                'sh', '-eu', '-c', $markerCommand
            )
        }
        Write-Output 'Restored volume verification passed: exact backup labels, expected service markers, and disconnected receipt.'
    }
    Write-Output "Recovery package verified: backup $($manifest.backupId)."
    Write-Output 'Verified authenticated encryption, outer and inner checksums, fixed project identity, and three expected payloads.'
} finally {
    try {
        Remove-RecoveryWorkingDirectory -Path $work
    } finally {
        [Array]::Clear($key, 0, $key.Length)
    }
}
