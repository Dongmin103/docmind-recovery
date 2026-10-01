[CmdletBinding()]
param(
    [string]$EnvironmentPath,
    [switch]$Force,
    [switch]$ApprovedDept2Reindex
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
if (-not $EnvironmentPath) {
    $EnvironmentPath = Join-Path $localRoot 'docker/windows-dev.env'
}
$resolvedEnvironment = [IO.Path]::GetFullPath($EnvironmentPath)
if (-not $resolvedEnvironment.StartsWith($localRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Development security files must stay below $localRoot"
}
if (-not (Test-Path -LiteralPath $resolvedEnvironment -PathType Leaf)) {
    throw "Missing local environment file. Run Initialize-WindowsDevelopmentDocker.ps1 first: $resolvedEnvironment"
}

$requiredPolicy = [ordered]@{
    DOCMIND_DEV_SECURITY_MODE = $(if ($ApprovedDept2Reindex) { 'isolated-approved-dept2-reindex' } else { 'isolated-synthetic-only' })
    DOCMIND_DEV_DATA_CLASS = $(if ($ApprovedDept2Reindex) { 'approved-dept2-reindex' } else { 'synthetic-only' })
    DOCMIND_DEV_ALLOW_PLAINTEXT_LOOPBACK = '1'
    DOCMIND_DEV_EXTERNAL_API_POLICY = 'https-only'
}
if ($ApprovedDept2Reindex) {
    $requiredPolicy.DOCMIND_DEV_APPROVED_SOURCE_ID = 'dept-2-e2e'
}
$lines = [Collections.Generic.List[string]]::new()
foreach ($line in [IO.File]::ReadAllLines($resolvedEnvironment)) {
    $lines.Add($line)
}

foreach ($entry in $requiredPolicy.GetEnumerator()) {
    $matchingIndexes = @()
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match "^$([Regex]::Escape($entry.Key))=(.*)$") {
            $matchingIndexes += $index
        }
    }
    if ($matchingIndexes.Count -gt 1) {
        throw "Duplicate security policy key in local environment: $($entry.Key)"
    }
    if ($matchingIndexes.Count -eq 0) {
        $lines.Add("$($entry.Key)=$($entry.Value)")
        continue
    }
    $current = $lines[$matchingIndexes[0]].Substring($entry.Key.Length + 1)
    if ($current -ne $entry.Value) {
        if (-not $Force) {
            throw "Refusing to replace $($entry.Key). Re-run with -Force to apply the selected boundary."
        }
        $lines[$matchingIndexes[0]] = "$($entry.Key)=$($entry.Value)"
    }
}

$content = ($lines -join [Environment]::NewLine).TrimEnd() + [Environment]::NewLine
[IO.File]::WriteAllText($resolvedEnvironment, $content, [Text.UTF8Encoding]::new($false))

# Restrict the ignored env file to the current Windows identity and LocalSystem.
# Docker Compose reads it as the current user; no broad Users/Administrators rule
# is needed for this development workflow.
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $currentIdentity.User
$aclOutput = (& icacls.exe $resolvedEnvironment `
    /inheritance:r `
    /grant:r "*$($currentSid.Value):(F)" '*S-1-5-18:(F)' 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Failed to restrict the local environment ACL: $aclOutput"
}

Write-Output "Applied the $(if ($ApprovedDept2Reindex) { 'approved DEPT2 reindex' } else { 'synthetic-only' }) security boundary to: $resolvedEnvironment"
Write-Output 'Restricted the local environment ACL to the current Windows identity and LocalSystem.'
Write-Output 'No certificate or production encryption claim was created.'
