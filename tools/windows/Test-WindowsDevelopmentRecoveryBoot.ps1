[CmdletBinding()]
param([string]$DockerCommand = 'docker')

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'WindowsDevelopmentRecovery.Common.ps1')

$repoRoot = Get-RecoveryRepositoryRoot
$work = New-RecoveryWorkingDirectory
try {
    $bridgeFixture = @'
value=$(printf '%s' "$EXPECTED_ID")
printf '%s\n' "$value" | grep -q '^[0-9a-f]\{32\}$'
'@
    $bridge = New-RecoveryDockerScriptInvocation `
        -ContainerId ('a' * 64) `
        -Environment ([ordered]@{
            EXPECTED_ID = '0123456789abcdef0123456789abcdef'
            EXPECTED_SHA = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        }) `
        -Script $bridgeFixture
    $decodedBridgeFixture = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($bridge.PayloadBase64)
    )
    $normalizedBridgeFixture = $bridgeFixture.Replace("`r`n", "`n").Replace("`r", "`n")
    $bridgeOuterArguments = @($bridge.Arguments | Select-Object -SkipLast 1)
    $bridgeDecoder = [string]$bridge.Arguments[-1]
    if ($decodedBridgeFixture -cne $normalizedBridgeFixture -or
        $decodedBridgeFixture.Contains("`r") -or
        @($bridgeOuterArguments | Where-Object { $_ -notmatch '^[A-Za-z0-9_.:/=@+-]+$' }).Count -ne 0 -or
        $bridgeDecoder -notmatch '^printf %s [A-Za-z0-9+/]+={0,2} \| base64 -d \| sh -eu$' -or
        ($bridge.Arguments -join ' ') -match [Regex]::Escape($bridgeFixture)) {
        throw 'Recovery Docker bridge exposed shell-sensitive script text in outer argv.'
    }
    & wsl.exe -d Ubuntu -- sh -c "printf %s $($bridge.PayloadBase64) | base64 -d | sh -n"
    if ($LASTEXITCODE -ne 0) {
        throw 'LF-normalized recovery Docker script failed POSIX sh syntax validation.'
    }
    $bridgeProbe = Join-Path $work 'docker-bridge-probe.cmd'
    $bridgeArgumentsPath = Join-Path $work 'docker-bridge-arguments.txt'
    [IO.File]::WriteAllLines(
        $bridgeProbe,
        @(
            '@echo off',
            'setlocal DisableDelayedExpansion',
            ">`"$bridgeArgumentsPath`" echo(%*",
            'exit /b 0'
        ),
        [Text.Encoding]::ASCII
    )
    Invoke-RecoveryDockerScript `
        -DockerCommand $bridgeProbe `
        -ContainerId ('a' * 64) `
        -Environment ([ordered]@{
            EXPECTED_ID = '0123456789abcdef0123456789abcdef'
            EXPECTED_SHA = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        }) `
        -Script $bridgeFixture
    $capturedBridgeArguments = (Get-Content -LiteralPath $bridgeArgumentsPath -Raw).Trim()
    if ($capturedBridgeArguments -notmatch '^exec -e EXPECTED_ID=[0-9a-f]{32} -e EXPECTED_SHA=[0-9a-f]{64} [a-f]{64} sh -c "?printf %s [A-Za-z0-9+/]+={0,2} \| base64 -d \| sh -eu"?$' -or
        $capturedBridgeArguments.Contains('value=$(printf') -or
        $capturedBridgeArguments.Contains('EXPECTED_ID)')) {
        throw 'Windows cmd bridge exposed raw shell text or escaped the fixed base64 decoder.'
    }
    $bootVerifierSource = Get-Content -LiteralPath (
        Join-Path $PSScriptRoot 'Invoke-WindowsDevelopmentRecoveryBoot.ps1'
    ) -Raw
    if ($bootVerifierSource -match '\|\s*grep\b' -or
        $bootVerifierSource -notmatch 'MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\"' -or
        $bootVerifierSource -notmatch 'case \"\$body\" in' -or
        $bootVerifierSource -notmatch 'case \"\$actual_sha\" in') {
        throw 'Recovery marker verification must use image-portable variable and POSIX case checks without grep.'
    }
    $verifierTokens = $null
    $verifierErrors = $null
    $verifierAst = [Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $PSScriptRoot 'Invoke-WindowsDevelopmentRecoveryBoot.ps1'),
        [ref]$verifierTokens,
        [ref]$verifierErrors
    )
    if ($verifierErrors.Count -ne 0) {
        throw 'Recovery boot verifier cannot be parsed for shell payload validation.'
    }
    $verificationPayloadNodes = @($verifierAst.FindAll({
        param($node)
        $node -is [Management.Automation.Language.StringConstantExpressionAst] -and
            $node.StringConstantType -eq [Management.Automation.Language.StringConstantType]::SingleQuotedHereString -and
            $node.Value.Contains('EXPECTED_ID')
    }, $true))
    if ($verificationPayloadNodes.Count -ne 3) {
        throw 'Recovery boot verifier must contain exactly three marker verification payloads.'
    }
    foreach ($payloadNode in $verificationPayloadNodes) {
        $payloadInvocation = New-RecoveryDockerScriptInvocation `
            -ContainerId ('a' * 64) `
            -Environment ([ordered]@{
                EXPECTED_ID = '0123456789abcdef0123456789abcdef'
                EXPECTED_SHA = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
            }) `
            -Script $payloadNode.Value
        $decodedPayload = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($payloadInvocation.PayloadBase64)
        )
        if ($decodedPayload.Contains("`r")) {
            throw 'Recovery marker verification payload retained a carriage return after encoding.'
        }
        & wsl.exe -d Ubuntu -- sh -c "printf %s $($payloadInvocation.PayloadBase64) | base64 -d | sh -n"
        if ($LASTEXITCODE -ne 0) {
            throw 'A recovery marker verification payload failed POSIX sh syntax validation.'
        }
    }

    $environmentPath = Join-Path $work 'compose.env'
    $digest = 'sha256:' + ('1' * 64)
    $environment = @(
        'RECOVERY_COMPOSE_PROJECT=docmind-recovery-boot-selftest',
        'RECOVERY_BACKUP_ID=00000000000000000000000000000000',
        "RECOVERY_MYSQL_IMAGE=$digest",
        "RECOVERY_ELASTICSEARCH_IMAGE=$digest",
        "RECOVERY_MINIO_IMAGE=$digest",
        'RECOVERY_MYSQL_VOLUME=docmind-recovery-selftest-mysql',
        'RECOVERY_ES_VOLUME=docmind-recovery-selftest-es',
        'RECOVERY_MINIO_VOLUME=docmind-recovery-selftest-minio',
        'RECOVERY_MYSQL_PORT=23306',
        'RECOVERY_ES_PORT=29200',
        'RECOVERY_MINIO_PORT=29000',
        'RECOVERY_MINIO_CONSOLE_PORT=29001',
        'MYSQL_PASSWORD=00000000000000000000000000000000',
        'ELASTIC_PASSWORD=00000000000000000000000000000000',
        'MINIO_USER=docmindselftest',
        'MINIO_PASSWORD=00000000000000000000000000000000',
        'TZ=Asia/Seoul'
    )
    [IO.File]::WriteAllLines($environmentPath, $environment, [Text.UTF8Encoding]::new($false))
    $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $relativeEnvironment = $environmentPath.Substring($repoPrefix.Length).Replace('\', '/')
    Push-Location $repoRoot
    try {
        $jsonText = (& $DockerCommand compose `
            --env-file $relativeEnvironment `
            -f app/docker/docker-compose-windows-recovery-boot.yml `
            config --format json 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { throw "Recovery boot Compose self-test failed without starting services: $jsonText" }
        $config = $jsonText | ConvertFrom-Json
    } finally {
        Pop-Location
    }

    if ($config.name -ne 'docmind-recovery-boot-selftest' -or
        @($config.services.PSObject.Properties).Count -ne 3 -or
        @($config.networks.PSObject.Properties).Count -ne 1) {
        throw 'Recovery boot Compose is not an isolated three-service, one-network project.'
    }
    foreach ($serviceProperty in $config.services.PSObject.Properties) {
        $service = $serviceProperty.Value
        if ($service.pull_policy -ne 'never' -or
            $service.security_opt -notcontains 'no-new-privileges:true' -or
            [string]$service.labels.'com.docmind.recovery-boot' -ne 'true') {
            throw "Recovery boot service hardening is incomplete: $($serviceProperty.Name)"
        }
        foreach ($port in @($service.ports)) {
            if ($port.host_ip -ne '127.0.0.1') {
                throw "Recovery boot service escaped loopback: $($serviceProperty.Name)"
            }
        }
        foreach ($mount in @($service.volumes)) {
            if ($mount.type -ne 'volume' -or [string]$mount.source -notin @('mysql_recovery', 'es_recovery', 'minio_recovery')) {
                throw "Recovery boot service has a non-recovery mount: $($serviceProperty.Name)/$($mount.source)"
            }
        }
    }
    $networkProperty = @($config.networks.PSObject.Properties)[0]
    $network = $networkProperty.Value
    if (-not $network.internal -or [string]$network.labels.'com.docmind.recovery-boot' -ne 'true') {
        throw 'Recovery boot network must be internal and recovery-labeled.'
    }
    foreach ($volumeProperty in $config.volumes.PSObject.Properties) {
        if (-not $volumeProperty.Value.external -or
            [string]$volumeProperty.Value.name -notlike 'docmind-recovery-selftest-*') {
            throw 'Recovery boot Compose must use only explicitly named external recovery volumes.'
        }
    }
    Write-Output 'Recovery boot static self-test passed without starting containers or accessing volumes.'
    Write-Output 'Verified: cmd/WSL-safe script argv, three services, local images only, loopback ports, one internal network, and external recovery volumes.'
} finally {
    Remove-RecoveryWorkingDirectory -Path $work
}
