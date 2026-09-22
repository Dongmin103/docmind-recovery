[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
$work = Join-Path $localRoot ('recovery-marker-selftest-' + [Guid]::NewGuid().ToString('N'))
$localPrefix = $localRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $work.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Self-test work directory escaped .local.'
}

try {
    [IO.Directory]::CreateDirectory($work) | Out-Null
    $environmentPath = Join-Path $work 'windows-dev.env'
    $receiptPath = Join-Path $work 'marker.json'
    $callLog = Join-Path $work 'docker-calls.log'
    $fakeDocker = Join-Path $work 'fake-docker.ps1'
    [IO.File]::WriteAllLines(
        $environmentPath,
        @(
            'DOCMIND_DEV_SECURITY_MODE=isolated-synthetic-only',
            'DOCMIND_DEV_DATA_CLASS=synthetic-only'
        ),
        [Text.UTF8Encoding]::new($false)
    )
    $fakeDockerSource = @'
$ErrorActionPreference = 'Stop'
$argumentsText = @($args)
if ($argumentsText[0] -eq 'compose') {
    $service = $argumentsText[-1]
    [IO.File]::AppendAllText($env:DOCMIND_MARKER_SELFTEST_LOG, "compose:ps:$service`n")
    Write-Output "container-$service"
    exit 0
}
if ($argumentsText[0] -eq 'inspect') {
    $id = $argumentsText[1]
    $service = $id.Replace('container-', '')
    $volume = switch ($service) {
        mysql { 'docmind-windows-dev_mysql_data' }
        es01 { 'docmind-windows-dev_es_data' }
        minio { 'docmind-windows-dev_minio_data' }
        default { exit 41 }
    }
    $target = switch ($service) {
        mysql { '/var/lib/mysql' }
        es01 { '/usr/share/elasticsearch/data' }
        minio { '/data' }
    }
    [IO.File]::AppendAllText($env:DOCMIND_MARKER_SELFTEST_LOG, "inspect:$service`n")
    @([ordered]@{
        Config = [ordered]@{ Labels = [ordered]@{
            'com.docker.compose.project' = 'docmind-windows-dev'
            'com.docker.compose.service' = $service
        } }
        State = [ordered]@{ Status = 'running'; Health = [ordered]@{ Status = 'healthy' } }
        Mounts = @([ordered]@{ Type = 'volume'; Name = $volume; Destination = $target })
    }) | ConvertTo-Json -Depth 8 -Compress
    exit 0
}
if ($argumentsText[0] -eq 'volume' -and $argumentsText[1] -eq 'inspect') {
    $name = $argumentsText[2]
    $logical = $name.Replace('docmind-windows-dev_', '')
    [IO.File]::AppendAllText($env:DOCMIND_MARKER_SELFTEST_LOG, "volume:inspect:$name`n")
    @([ordered]@{
        Name = $name
        Labels = [ordered]@{
            'com.docker.compose.project' = 'docmind-windows-dev'
            'com.docker.compose.volume' = $logical
        }
    }) | ConvertTo-Json -Depth 5 -Compress
    exit 0
}
if ($argumentsText[0] -eq 'exec') {
    $container = @($argumentsText | Where-Object { $_ -like 'container-*' })[0]
    $wrapper = $argumentsText[-1]
    if ($argumentsText[-3] -ne 'sh' -or $argumentsText[-2] -ne '-c' -or
        $wrapper -notmatch '^printf %s ([A-Za-z0-9+/]+={0,2}) \| base64 -d \| sh -eu$') {
        exit 43
    }
    $decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[1]))
    $expectedPrefix = switch ($container) {
        'container-mysql' { 'export MYSQL_PWD=' }
        'container-es01' { 'printf ' }
        'container-minio' { 'mc alias ' }
        default { exit 44 }
    }
    if (-not $decoded.StartsWith($expectedPrefix) -or $decoded.Contains("`r") -or
        $decoded -match '[A-Fa-f0-9]{32,}') {
        exit 45
    }
    [IO.File]::AppendAllText($env:DOCMIND_MARKER_SELFTEST_LOG, "exec:$container`:base64`n")
    exit 0
}
exit 42
'@
    [IO.File]::WriteAllText($fakeDocker, $fakeDockerSource, [Text.UTF8Encoding]::new($false))
    $oldLog = $env:DOCMIND_MARKER_SELFTEST_LOG
    $env:DOCMIND_MARKER_SELFTEST_LOG = $callLog
    try {
        $unsafeEnvironmentPath = Join-Path $work 'unsafe.env'
        [IO.File]::WriteAllLines(
            $unsafeEnvironmentPath,
            @(
                'DOCMIND_DEV_SECURITY_MODE=isolated-synthetic-only',
                'DOCMIND_DEV_DATA_CLASS=operating-data'
            ),
            [Text.UTF8Encoding]::new($false)
        )
        $unsafeRejected = $false
        try {
            & (Join-Path $PSScriptRoot 'New-WindowsDevelopmentRecoveryMarker.ps1') `
                -OutputPath (Join-Path $work 'unsafe-marker.json') `
                -EnvironmentPath $unsafeEnvironmentPath `
                -DockerCommand $fakeDocker `
                -ValidateOnly
        } catch {
            $unsafeRejected = $_.Exception.Message -like '*synthetic-only*'
        }
        if (-not $unsafeRejected -or (Test-Path -LiteralPath $callLog)) {
            throw 'Recovery marker did not reject a non-synthetic policy before Docker access.'
        }

        & (Join-Path $PSScriptRoot 'New-WindowsDevelopmentRecoveryMarker.ps1') `
            -OutputPath $receiptPath `
            -EnvironmentPath $environmentPath `
            -DockerCommand $fakeDocker `
            -Confirm:$false
    } finally {
        $env:DOCMIND_MARKER_SELFTEST_LOG = $oldLog
    }

    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    if ($receipt.format -ne 'docmind-recovery-marker/v1' -or
        $receipt.composeProject -ne 'docmind-windows-dev' -or
        [string]$receipt.markerId -notmatch '^[0-9a-f]{32}$' -or
        [string]$receipt.sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Recovery marker receipt contract is invalid.'
    }
    $receiptProperties = @($receipt.PSObject.Properties.Name | Sort-Object)
    if (($receiptProperties -join ',') -ne 'composeProject,format,markerId,sha256') {
        throw 'Recovery marker receipt persisted an unexpected field.'
    }
    $calls = @(Get-Content -LiteralPath $callLog)
    if (@($calls | Where-Object { $_ -like 'compose:ps:*' }).Count -ne 3 -or
        @($calls | Where-Object { $_ -like 'inspect:*' }).Count -ne 3 -or
        @($calls | Where-Object { $_ -like 'volume:inspect:*' }).Count -ne 3 -or
        @($calls | Where-Object { $_ -like 'exec:*' }).Count -ne 3 -or
        @($calls | Where-Object { $_ -match 'recovery|source|remove|rm' }).Count -ne 0) {
        throw 'Recovery marker self-test observed an unsafe or incomplete Docker command plan.'
    }
    Write-Output 'Recovery marker self-test passed without contacting Docker or persistent volumes.'
    Write-Output 'Verified: synthetic-only policy, exact project labels, healthy services, three marker writes, and metadata-only receipt.'
} finally {
    if (Test-Path -LiteralPath $work) {
        $resolvedWork = [IO.Path]::GetFullPath($work)
        if (-not $resolvedWork.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to remove a self-test path outside .local.'
        }
        [IO.Directory]::Delete($resolvedWork, $true)
    }
}
