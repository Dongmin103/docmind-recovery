[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')]
    [string]$ExpectedDnsName,
    [Parameter(Mandatory = $true)]
    [string]$DockerStoragePath,
    [string]$OutputPath,
    [ValidateRange(1, 365)]
    [int]$MinimumValidityDays = 30,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$localSecurityRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local/security'))
if (-not $OutputPath) {
    $OutputPath = Join-Path $localSecurityRoot 'windows-security-preflight.env'
}
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
if (-not $resolvedOutput.StartsWith($localSecurityRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Production security preflight files must stay below $localSecurityRoot"
}
if ((Test-Path -LiteralPath $resolvedOutput) -and -not $Force) {
    throw "Refusing to overwrite $resolvedOutput. Re-run with -Force only after reviewing the local evidence paths."
}

$resolvedDockerStorage = [IO.Path]::GetFullPath($DockerStoragePath)
if (-not (Test-Path -LiteralPath $resolvedDockerStorage)) {
    throw 'DockerStoragePath must identify the actual existing Docker Desktop or WSL data location.'
}

[IO.Directory]::CreateDirectory($localSecurityRoot) | Out-Null
$content = @"
DOCMIND_SECURITY_PREFLIGHT_MODE=certificate-prerequisite-only
DOCMIND_TLS_EXPECTED_DNS_NAME=$ExpectedDnsName
DOCMIND_TLS_MIN_VALID_DAYS=$MinimumValidityDays
DOCMIND_TLS_CA_CERT_FILE=../../.local/security/ca.pem
DOCMIND_TLS_SERVER_CERT_FILE=../../.local/security/server.pem
DOCMIND_TLS_SERVER_KEY_FILE=../../.local/security/server.key
DOCMIND_TLS_ROTATION_EVIDENCE_FILE=../../.local/security/tls-rotation-evidence.json
DOCMIND_SECRET_PROVIDER_EVIDENCE_FILE=../../.local/security/secret-provider-evidence.json
DOCMIND_DOCKER_STORAGE_PATH=$($resolvedDockerStorage.Replace('\', '/'))
"@
[IO.File]::WriteAllText($resolvedOutput, $content, [Text.UTF8Encoding]::new($false))

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$aclOutput = (& icacls.exe $resolvedOutput /inheritance:r /grant:r "*$currentSid`:(F)" '*S-1-5-18:(F)' 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Failed to restrict the preflight env ACL: $aclOutput"
}

Write-Output "Created ignored production-security preflight configuration: $resolvedOutput"
Write-Output 'No certificate, private key, rotation evidence, secret-provider evidence, TLS runtime wiring, or storage-encryption approval was created.'
Write-Output 'The generated mode is certificate-prerequisite-only and cannot make the development Compose production-capable.'
