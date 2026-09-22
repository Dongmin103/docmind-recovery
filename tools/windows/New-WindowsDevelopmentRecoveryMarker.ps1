[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string]$OutputPath,
    [string]$EnvironmentPath,
    [string]$DockerCommand = 'docker',
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$project = 'docmind-windows-dev'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
$localPrefix = $localRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $EnvironmentPath) { $EnvironmentPath = Join-Path $localRoot 'docker/windows-dev.env' }
$resolvedEnvironment = [IO.Path]::GetFullPath($EnvironmentPath)
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)

function Read-MarkerEnvironment([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed -notmatch '^([A-Z][A-Z0-9_]*)=(.*)$') {
            throw 'Development environment contains an unsupported line.'
        }
        if ($values.ContainsKey($Matches[1])) {
            throw "Development environment contains a duplicate key: $($Matches[1])"
        }
        $values[$Matches[1]] = $Matches[2]
    }
    return $values
}

function Invoke-MarkerDocker {
    param(
        [Parameter(Mandatory)] [string[]]$Arguments,
        [switch]$Capture
    )
    if ($Capture) {
        $output = (& $DockerCommand @Arguments 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Docker marker validation command failed.' }
        return $output
    }
    & $DockerCommand @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker marker operation failed.' }
}

function ConvertTo-MarkerShellPayload([string]$ScriptText) {
    $normalized = $ScriptText.Replace("`r`n", "`n").Replace("`r", "`n")
    $bytes = [Text.Encoding]::UTF8.GetBytes($normalized)
    try {
        return [Convert]::ToBase64String($bytes)
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Get-ServiceContainer {
    param(
        [Parameter(Mandatory)] [string]$Service,
        [Parameter(Mandatory)] [string]$ExpectedVolume,
        [Parameter(Mandatory)] [string]$ExpectedTarget
    )
    $containerId = Invoke-MarkerDocker -Arguments ($script:ComposePrefix + @(
        'ps', '--quiet', $Service
    )) -Capture
    if ($containerId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{5,127}$' -or $containerId -match "`r|`n") {
        throw "Cannot resolve exactly one development container for $Service."
    }
    $inspection = Invoke-MarkerDocker -Arguments @('inspect', $containerId) -Capture | ConvertFrom-Json
    if (@($inspection).Count -ne 1) { throw "Container inspection is ambiguous for $Service." }
    $container = $inspection[0]
    if ([string]$container.Config.Labels.'com.docker.compose.project' -ne $project -or
        [string]$container.Config.Labels.'com.docker.compose.service' -ne $Service) {
        throw "Container escaped the exact development Compose identity: $Service."
    }
    if ([string]$container.State.Status -ne 'running' -or
        [string]$container.State.Health.Status -ne 'healthy') {
        throw "Development service is not healthy: $Service."
    }
    $mounts = @($container.Mounts | Where-Object {
        $_.Type -eq 'volume' -and $_.Name -eq $ExpectedVolume -and $_.Destination -eq $ExpectedTarget
    })
    if ($mounts.Count -ne 1) {
        throw "Container does not use the exact expected development volume: $Service."
    }
    return $containerId
}

function Assert-DevelopmentVolume {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$ComposeVolume
    )
    $inspection = Invoke-MarkerDocker -Arguments @('volume', 'inspect', $Name) -Capture | ConvertFrom-Json
    if (@($inspection).Count -ne 1) { throw "Volume inspection is ambiguous: $Name" }
    $labels = $inspection[0].Labels
    if ([string]$inspection[0].Name -ne $Name -or
        [string]$labels.'com.docker.compose.project' -ne $project -or
        [string]$labels.'com.docker.compose.volume' -ne $ComposeVolume) {
        throw "Volume escaped the exact development Compose identity: $Name"
    }
}

if (-not $resolvedEnvironment.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-Path -LiteralPath $resolvedEnvironment -PathType Leaf)) {
    throw 'Recovery marker accepts only an existing ignored environment below .local.'
}
if (-not $resolvedOutput.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Recovery marker receipt must remain below the ignored .local directory.'
}
if (Test-Path -LiteralPath $resolvedOutput) {
    throw "Refusing to overwrite an existing recovery marker receipt: $resolvedOutput"
}
$environment = Read-MarkerEnvironment $resolvedEnvironment
if ($environment.DOCMIND_DEV_SECURITY_MODE -ne 'isolated-synthetic-only' -or
    $environment.DOCMIND_DEV_DATA_CLASS -ne 'synthetic-only') {
    throw 'Recovery markers are allowed only in the isolated synthetic-only development environment.'
}

$relativeEnvironment = $resolvedEnvironment.Substring(
    $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar).Length + 1
).Replace('\', '/')
$script:ComposePrefix = @(
    'compose', '-p', $project,
    '--env-file', $relativeEnvironment,
    '-f', 'app/docker/docker-compose-windows-dev.yml',
    '--profile', 'infra'
)
$volumeMap = [ordered]@{
    mysql = [ordered]@{ name = 'docmind-windows-dev_mysql_data'; logical = 'mysql_data'; target = '/var/lib/mysql' }
    es01 = [ordered]@{ name = 'docmind-windows-dev_es_data'; logical = 'es_data'; target = '/usr/share/elasticsearch/data' }
    minio = [ordered]@{ name = 'docmind-windows-dev_minio_data'; logical = 'minio_data'; target = '/data' }
}

Push-Location $repoRoot
try {
    $containers = [ordered]@{}
    foreach ($entry in $volumeMap.GetEnumerator()) {
        Assert-DevelopmentVolume -Name $entry.Value.name -ComposeVolume $entry.Value.logical
        $containers[$entry.Key] = Get-ServiceContainer `
            -Service $entry.Key `
            -ExpectedVolume $entry.Value.name `
            -ExpectedTarget $entry.Value.target
    }
    Write-Output 'Verified three healthy containers and exact docmind-windows-dev volume labels.'
    if ($ValidateOnly) {
        Write-Output 'Recovery marker validation passed; no marker or receipt was written.'
        return
    }
    if (-not $PSCmdlet.ShouldProcess($project, 'write one synthetic recovery marker to MySQL, Elasticsearch, and MinIO')) {
        return
    }

    $parent = Split-Path -Parent $resolvedOutput
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $markerId = [Guid]::NewGuid().ToString('N')
    $markerBytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $sha = [Security.Cryptography.SHA256]::Create()
    $markerBase64 = $null
    try {
        $rng.GetBytes($markerBytes)
        $markerSha256 = -join ($sha.ComputeHash($markerBytes) | ForEach-Object { $_.ToString('x2') })
        $markerBase64 = [Convert]::ToBase64String($markerBytes)

        $mysqlPayload = ConvertTo-MarkerShellPayload @'
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
mysql -uroot -e "CREATE DATABASE IF NOT EXISTS docmind_recovery_probe; CREATE TABLE IF NOT EXISTS docmind_recovery_probe.markers (marker_id CHAR(32) PRIMARY KEY, marker_sha256 CHAR(64) NOT NULL); INSERT INTO docmind_recovery_probe.markers (marker_id, marker_sha256) VALUES ('$MARKER_ID', '$MARKER_SHA') ON DUPLICATE KEY UPDATE marker_sha256=VALUES(marker_sha256);"
test "$(mysql -uroot -Nse "SELECT COUNT(*) FROM docmind_recovery_probe.markers WHERE marker_id = '$MARKER_ID' AND marker_sha256 = '$MARKER_SHA';")" = 1
'@
        $elasticsearchPayload = ConvertTo-MarkerShellPayload @'
printf '{"marker_sha256":"%s"}' "$MARKER_SHA" | curl -fsS -u elastic:"$ELASTIC_PASSWORD" -H 'Content-Type: application/json' -XPUT "http://127.0.0.1:9200/docmind-recovery-probe/_doc/$MARKER_ID?refresh=wait_for" --data-binary @- >/dev/null
body=$(curl -fsS -u elastic:"$ELASTIC_PASSWORD" "http://127.0.0.1:9200/docmind-recovery-probe/_doc/$MARKER_ID")
expected_found='"found":true'
expected_hash="\"marker_sha256\":\"$MARKER_SHA\""
case "$body" in *"$expected_found"*) : ;; *) exit 1 ;; esac
case "$body" in *"$expected_hash"*) : ;; *) exit 1 ;; esac
'@
        $minioPayload = ConvertTo-MarkerShellPayload @'
mc alias set development http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing development/docmind-recovery-probe >/dev/null
printf '%s' "$MARKER_BASE64" | base64 -d | mc pipe "development/docmind-recovery-probe/$MARKER_ID" >/dev/null
actual_sha=$(mc cat "development/docmind-recovery-probe/$MARKER_ID" | sha256sum)
case "$actual_sha" in "$MARKER_SHA "*) : ;; *) exit 1 ;; esac
'@

        Invoke-MarkerDocker -Arguments @(
            'exec', '-e', "MARKER_ID=$markerId", '-e', "MARKER_SHA=$markerSha256", $containers.mysql,
            'sh', '-c', "printf %s $mysqlPayload | base64 -d | sh -eu"
        )
        Invoke-MarkerDocker -Arguments @(
            'exec', '-e', "MARKER_ID=$markerId", '-e', "MARKER_SHA=$markerSha256", $containers.es01,
            'sh', '-c', "printf %s $elasticsearchPayload | base64 -d | sh -eu"
        )
        Invoke-MarkerDocker -Arguments @(
            'exec', '-e', "MARKER_ID=$markerId", '-e', "MARKER_SHA=$markerSha256", '-e', "MARKER_BASE64=$markerBase64", $containers.minio,
            'sh', '-c', "printf %s $minioPayload | base64 -d | sh -eu"
        )

        $temporaryReceipt = Join-Path $parent ('.' + [IO.Path]::GetFileName($resolvedOutput) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
        try {
            $receipt = [ordered]@{
                format = 'docmind-recovery-marker/v1'
                composeProject = $project
                markerId = $markerId
                sha256 = $markerSha256
            }
            [IO.File]::WriteAllText(
                $temporaryReceipt,
                ($receipt | ConvertTo-Json -Depth 3),
                [Text.UTF8Encoding]::new($false)
            )
            [IO.File]::Move($temporaryReceipt, $resolvedOutput)
        } finally {
            if (Test-Path -LiteralPath $temporaryReceipt) {
                [IO.File]::Delete($temporaryReceipt)
            }
        }
    } finally {
        if ($rng) { $rng.Dispose() }
        if ($sha) { $sha.Dispose() }
        [Array]::Clear($markerBytes, 0, $markerBytes.Length)
        $markerBase64 = $null
        $mysqlPayload = $null
        $elasticsearchPayload = $null
        $minioPayload = $null
    }
} finally {
    Pop-Location
}

Write-Output "Synthetic recovery marker receipt created: $resolvedOutput"
Write-Output 'Marker bytes and credentials were not written to the console or receipt.'
