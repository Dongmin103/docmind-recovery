Set-StrictMode -Version Latest

$script:RecoveryFormat = 'docmind-windows-dev-recovery/v1'
$script:RecoveryProject = 'docmind-windows-dev'
$script:RecoveryMagic = [Text.Encoding]::ASCII.GetBytes('DMRECOV1')
$script:RecoveryHelperImage = 'busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662'

function Get-RecoveryRepositoryRoot {
    return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
}

function Assert-RecoveryPath {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Purpose,
        [switch]$MustExist
    )

    $resolved = [IO.Path]::GetFullPath($Path)
    $forbidden = @(
        'D:\UPLEXSOFT\UDRIVE\USER\TEST1',
        'D:\UPLEXSOFT\UDRIVE\DEPT\_DEPT_2',
        'D:\UPLEXSOFT\UDRIVE\DEPT\_DEPT_1'
    )
    foreach ($root in $forbidden) {
        if ($resolved.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
            $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "$Purpose must not use an original source root: $resolved"
        }
    }

    $repoRoot = Get-RecoveryRepositoryRoot
    $localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
    $insideRepository = $resolved.StartsWith(
        $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
    $insideLocal = $resolved.StartsWith(
        $localRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
    if ($insideRepository -and -not $insideLocal) {
        throw "$Purpose must be outside the repository or below the ignored .local directory: $resolved"
    }
    if ($MustExist -and -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Purpose does not exist: $resolved"
    }
    return $resolved
}

function Read-RecoveryKey {
    param([Parameter(Mandatory)] [string]$KeyPath)

    $resolved = Assert-RecoveryPath -Path $KeyPath -Purpose 'Recovery key' -MustExist
    $keyDocument = Get-Content -LiteralPath $resolved -Raw | ConvertFrom-Json
    if ($keyDocument.format -ne 'docmind-recovery-key/v1') {
        throw 'Unsupported recovery key format.'
    }
    try {
        $key = [Convert]::FromBase64String([string]$keyDocument.key)
    } catch {
        throw 'Recovery key is not valid base64.'
    }
    if ($key.Length -ne 64) {
        throw 'Recovery key must contain exactly 64 random bytes.'
    }
    return $key
}

function Get-FileSha256 {
    param([Parameter(Mandatory)] [string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-RecoveryBytesEqual {
    param(
        [Parameter(Mandatory)] [byte[]]$Left,
        [Parameter(Mandatory)] [byte[]]$Right
    )

    if ($Left.Length -ne $Right.Length) { return $false }
    $difference = 0
    for ($index = 0; $index -lt $Left.Length; $index++) {
        $difference = $difference -bor ($Left[$index] -bxor $Right[$index])
    }
    return $difference -eq 0
}

function Protect-RecoveryFile {
    param(
        [Parameter(Mandatory)] [string]$InputPath,
        [Parameter(Mandatory)] [string]$OutputPath,
        [Parameter(Mandatory)] [byte[]]$Key
    )

    if (Test-Path -LiteralPath $OutputPath) {
        throw "Refusing to overwrite encrypted recovery package: $OutputPath"
    }
    $temporaryCipher = "$OutputPath.cipher.tmp"
    $temporaryPackage = "$OutputPath.package.tmp"
    $aes = [Security.Cryptography.Aes]::Create()
    try {
        $aes.Mode = [Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $Key[0..31]
        $aes.GenerateIV()

        $input = [IO.File]::OpenRead($InputPath)
        $cipherOutput = [IO.File]::Create($temporaryCipher)
        try {
            $encryptor = $aes.CreateEncryptor()
            $crypto = [Security.Cryptography.CryptoStream]::new(
                $cipherOutput,
                $encryptor,
                [Security.Cryptography.CryptoStreamMode]::Write
            )
            try {
                $input.CopyTo($crypto)
                $crypto.FlushFinalBlock()
            } finally {
                $crypto.Dispose()
                $encryptor.Dispose()
            }
        } finally {
            $input.Dispose()
            $cipherOutput.Dispose()
        }

        $header = [byte[]]::new($script:RecoveryMagic.Length + $aes.IV.Length)
        [Array]::Copy($script:RecoveryMagic, 0, $header, 0, $script:RecoveryMagic.Length)
        [Array]::Copy($aes.IV, 0, $header, $script:RecoveryMagic.Length, $aes.IV.Length)
        $package = [IO.File]::Create($temporaryPackage)
        $hmac = [Security.Cryptography.HMACSHA256]::new($Key[32..63])
        try {
            $package.Write($header, 0, $header.Length)
            [void]$hmac.TransformBlock($header, 0, $header.Length, $null, 0)
            $cipherInput = [IO.File]::OpenRead($temporaryCipher)
            try {
                $buffer = [byte[]]::new(1MB)
                while (($read = $cipherInput.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $package.Write($buffer, 0, $read)
                    [void]$hmac.TransformBlock($buffer, 0, $read, $null, 0)
                }
            } finally {
                $cipherInput.Dispose()
            }
            [void]$hmac.TransformFinalBlock([byte[]]::new(0), 0, 0)
            $package.Write($hmac.Hash, 0, $hmac.Hash.Length)
        } finally {
            $hmac.Dispose()
            $package.Dispose()
        }
        Move-Item -LiteralPath $temporaryPackage -Destination $OutputPath
    } finally {
        $aes.Dispose()
        Remove-Item -LiteralPath $temporaryCipher -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $temporaryPackage -Force -ErrorAction SilentlyContinue
    }
}

function Unprotect-RecoveryFile {
    param(
        [Parameter(Mandatory)] [string]$InputPath,
        [Parameter(Mandatory)] [string]$OutputPath,
        [Parameter(Mandatory)] [byte[]]$Key
    )

    if (Test-Path -LiteralPath $OutputPath) {
        throw "Refusing to overwrite decrypted temporary file: $OutputPath"
    }
    $headerLength = $script:RecoveryMagic.Length + 16
    $file = [IO.File]::OpenRead($InputPath)
    try {
        if ($file.Length -le ($headerLength + 32)) {
            throw 'Recovery package is truncated.'
        }
        $authenticatedLength = $file.Length - 32
        $hmac = [Security.Cryptography.HMACSHA256]::new($Key[32..63])
        try {
            $buffer = [byte[]]::new(1MB)
            $remaining = $authenticatedLength
            while ($remaining -gt 0) {
                $wanted = [Math]::Min($buffer.Length, $remaining)
                $read = $file.Read($buffer, 0, $wanted)
                if ($read -le 0) { throw 'Recovery package ended before its authentication tag.' }
                [void]$hmac.TransformBlock($buffer, 0, $read, $null, 0)
                $remaining -= $read
            }
            [void]$hmac.TransformFinalBlock([byte[]]::new(0), 0, 0)
            $expected = [byte[]]::new(32)
            if ($file.Read($expected, 0, $expected.Length) -ne $expected.Length) {
                throw 'Recovery package authentication tag is incomplete.'
            }
            if (-not (Test-RecoveryBytesEqual -Left $hmac.Hash -Right $expected)) {
                throw 'Recovery package authentication failed. The file or key is wrong.'
            }
        } finally {
            $hmac.Dispose()
        }

        $file.Position = 0
        $header = [byte[]]::new($headerLength)
        if ($file.Read($header, 0, $header.Length) -ne $header.Length) {
            throw 'Recovery package header is incomplete.'
        }
        for ($index = 0; $index -lt $script:RecoveryMagic.Length; $index++) {
            if ($header[$index] -ne $script:RecoveryMagic[$index]) {
                throw 'Recovery package has an unsupported header.'
            }
        }
        $iv = $header[$script:RecoveryMagic.Length..($headerLength - 1)]
        $cipherLength = $authenticatedLength - $headerLength
        $temporaryCipher = "$OutputPath.cipher.tmp"
        $cipherOutput = [IO.File]::Create($temporaryCipher)
        try {
            $buffer = [byte[]]::new(1MB)
            $remaining = $cipherLength
            while ($remaining -gt 0) {
                $wanted = [Math]::Min($buffer.Length, $remaining)
                $read = $file.Read($buffer, 0, $wanted)
                if ($read -le 0) { throw 'Recovery package ciphertext is truncated.' }
                $cipherOutput.Write($buffer, 0, $read)
                $remaining -= $read
            }
        } finally {
            $cipherOutput.Dispose()
        }
    } finally {
        $file.Dispose()
    }

    $aes = [Security.Cryptography.Aes]::Create()
    try {
        $aes.Mode = [Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $Key[0..31]
        $aes.IV = $iv
        $cipherInput = [IO.File]::OpenRead($temporaryCipher)
        $plainOutput = [IO.File]::Create($OutputPath)
        try {
            $decryptor = $aes.CreateDecryptor()
            $crypto = [Security.Cryptography.CryptoStream]::new(
                $cipherInput,
                $decryptor,
                [Security.Cryptography.CryptoStreamMode]::Read
            )
            try {
                $crypto.CopyTo($plainOutput)
            } finally {
                $crypto.Dispose()
                $decryptor.Dispose()
            }
        } finally {
            $cipherInput.Dispose()
            $plainOutput.Dispose()
        }
    } catch {
        Remove-Item -LiteralPath $OutputPath -Force -ErrorAction SilentlyContinue
        throw
    } finally {
        $aes.Dispose()
        Remove-Item -LiteralPath $temporaryCipher -Force -ErrorAction SilentlyContinue
    }
}

function Assert-ArchiveEntriesSafe {
    param([Parameter(Mandatory)] [string]$ArchivePath)

    $entries = @(& tar -tzf $ArchivePath)
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to list recovery archive entries.'
    }
    foreach ($entry in $entries) {
        $normalized = ([string]$entry).Replace('\', '/')
        if ($normalized.StartsWith('/') -or $normalized -match '(^|/)\.\.(/|$)' -or $normalized -match '^[A-Za-z]:') {
            throw "Unsafe path in recovery archive: $entry"
        }
    }
}

function Read-RecoveryManifest {
    param([Parameter(Mandatory)] [string]$PayloadRoot)

    $manifestPath = Join-Path $PayloadRoot 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Recovery package does not contain manifest.json.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format -ne $script:RecoveryFormat -or $manifest.composeProject -ne $script:RecoveryProject) {
        throw 'Recovery manifest is not for the isolated DocMind Windows development project.'
    }
    if (@($manifest.payloads).Count -ne 3) {
        throw 'Recovery manifest must contain exactly the MySQL, Elasticsearch, and MinIO payloads.'
    }
    $expectedNames = @('mysql-data.tgz', 'elasticsearch-data.tgz', 'minio-data.tgz')
    foreach ($payload in $manifest.payloads) {
        if ($expectedNames -notcontains [string]$payload.file) {
            throw "Unexpected payload in recovery manifest: $($payload.file)"
        }
        $payloadPath = Join-Path $PayloadRoot ([string]$payload.file)
        if (-not (Test-Path -LiteralPath $payloadPath -PathType Leaf)) {
            throw "Missing recovery payload: $($payload.file)"
        }
        if ((Get-FileSha256 $payloadPath) -ne [string]$payload.sha256) {
            throw "Recovery payload checksum mismatch: $($payload.file)"
        }
        if ((Get-Item -LiteralPath $payloadPath).Length -ne [long]$payload.bytes) {
            throw "Recovery payload size mismatch: $($payload.file)"
        }
    }
    return $manifest
}

function Invoke-RecoveryDocker {
    param(
        [Parameter(Mandatory)] [string]$DockerCommand,
        [Parameter(Mandatory)] [string[]]$Arguments,
        [switch]$Capture
    )

    if ($Capture) {
        $result = (& $DockerCommand @Arguments 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw "Docker command failed without emitting sensitive output." }
        return $result
    }
    & $DockerCommand @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. Review Docker Desktop logs; recovery scripts did not print data or credentials.' }
}

function Convert-RecoveryDockerBindPath {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [ValidateSet('Native', 'Wsl')] [string]$Style = 'Native'
    )

    $resolved = [IO.Path]::GetFullPath($Path)
    if ($Style -eq 'Native') { return $resolved }
    $portableWindowsPath = $resolved.Replace('\', '/')
    $converted = (& wsl.exe -d Ubuntu -- wslpath -a -u $portableWindowsPath 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $converted.StartsWith('/mnt/', [StringComparison]::Ordinal)) {
        throw 'Unable to convert the temporary recovery path for the WSL Docker client.'
    }
    return $converted
}

function New-RecoveryWorkingDirectory {
    $repoRoot = Get-RecoveryRepositoryRoot
    $workRoot = Join-Path $repoRoot '.local/recovery/work'
    [IO.Directory]::CreateDirectory($workRoot) | Out-Null
    $path = Join-Path $workRoot ([Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($path) | Out-Null
    return $path
}
