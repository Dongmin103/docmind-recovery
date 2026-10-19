[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$SettingsPath)
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
Set-StrictMode -Version Latest
$root=$PSScriptRoot
$settings=Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$manifest=Get-Content -LiteralPath (Join-Path $root 'runtime-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.variant -notin @('cpu','gpu')) { throw 'Unsupported package variant.' }
$local=Join-Path $root '.local'
if (Test-Path -LiteralPath (Join-Path $local 'runtime.env')) { throw 'An installation already exists. Existing keys and data were preserved.' }
foreach ($item in @('Sources','JinaApiKeyFile','AnswerBaseUrl','WebPort','ApiPort')) {
    if ($settings.PSObject.Properties.Name -notcontains $item) { throw "Missing setting: $item" }
}
$answerUri=[Uri]::new([string]$settings.AnswerBaseUrl)
if (-not $answerUri.IsAbsoluteUri -or $answerUri.Scheme -notin @('http','https') -or $answerUri.UserInfo -or $answerUri.Query -or $answerUri.Fragment -or $answerUri.AbsolutePath.TrimEnd('/') -ne '/v1' -or $answerUri.Authority -notmatch ':\d+$' -or [Uri]::CheckHostName($answerUri.DnsSafeHost) -eq [UriHostNameType]::Unknown) { throw 'AnswerBaseUrl must be the approved Qwen server HTTP(S) endpoint with an explicit port and /v1 path.' }
foreach ($port in @($settings.WebPort,$settings.ApiPort)) {
    if ([int]$port -lt 1024 -or [int]$port -gt 65535) { throw 'Published ports must be between 1024 and 65535.' }
}
if ([int]$settings.WebPort -eq [int]$settings.ApiPort) { throw 'Web and API ports must differ.' }
$ids=@{}
foreach ($source in @($settings.Sources)) {
    if ([string]$source.source_id -cnotmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$' -or ([string]$source.source_id).Length -gt 64 -or $ids.ContainsKey([string]$source.source_id)) { throw 'Each source needs a unique lowercase ID.' }
    if (-not (Test-Path -LiteralPath ([string]$source.root) -PathType Container)) { throw 'A configured source root is unavailable.' }
    if (-not [IO.Path]::IsPathRooted([string]$source.root) -or [string]::IsNullOrWhiteSpace([string]$source.display_name) -or ([string]$source.display_name).Length -gt 128) { throw 'Sources require an absolute root and a display name of 1-128 characters.' }
    $ids[[string]$source.source_id]=$true
}
if (@($settings.Sources).Count -gt 0) {
    foreach ($item in @('UEncryptorPath','WorkRoot','BackupExclusionAcknowledged')) {
        if ($settings.PSObject.Properties.Name -notcontains $item) { throw "Missing source setting: $item" }
    }
    if (-not [IO.Path]::IsPathRooted([string]$settings.WorkRoot)) { throw 'WorkRoot must be an absolute temporary plaintext path.' }
    $work=[IO.Path]::GetFullPath([string]$settings.WorkRoot).TrimEnd('\')
    foreach ($source in @($settings.Sources)) {
        $sourceRoot=[IO.Path]::GetFullPath([string]$source.root).TrimEnd('\')
        if ($work -eq $sourceRoot -or $work.StartsWith($sourceRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $sourceRoot.StartsWith($work+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'WorkRoot and source roots must not overlap.' }
    }
    if (-not (Test-Path -LiteralPath ([string]$settings.UEncryptorPath) -PathType Leaf)) { throw 'Installed uEncryptor2 executable was not found.' }
    if (-not $settings.BackupExclusionAcknowledged) { throw 'Confirm that the temporary plaintext work directory is excluded from backup in the server settings.' }
    $signature=Get-AuthenticodeSignature -LiteralPath ([string]$settings.UEncryptorPath)
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate) { throw 'The installed uEncryptor2 signature could not be verified.' }
}
$utf8=[Text.UTF8Encoding]::new($false)
[void](New-Item -ItemType Directory -Force -Path $local,(Join-Path $local 'secrets'),(Join-Path $local 'installation'))
$acl=Get-Acl -LiteralPath $local
$acl.SetAccessRuleProtection($true,$false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18')) {
    $identity=[Security.Principal.SecurityIdentifier]::new($sid)
    $rule=[Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $local -AclObject $acl
function New-SecretHex {
    $bytes=New-Object byte[] 32
    $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-','').ToLowerInvariant()
}
foreach ($name in @('jina-key')) {
    $value=[IO.File]::ReadAllText([IO.Path]::GetFullPath([string]$settings.JinaApiKeyFile)).Trim()
    if ($value.Length -lt 12 -or $value.Contains("`r") -or $value.Contains("`n")) { throw 'An external model key is empty or malformed.' }
    [IO.File]::WriteAllText((Join-Path $local "secrets/$name"),$value,$utf8)
}
foreach ($name in @('embedding-key','session-key')) { [IO.File]::WriteAllText((Join-Path $local "secrets/$name"),(New-SecretHex),$utf8) }
# The host protocol consumes a hex-encoded 32-byte HMAC secret.
[IO.File]::WriteAllText((Join-Path $local 'secrets/worker-key'),(New-SecretHex),$utf8)
$device=if ($manifest.variant -eq 'gpu') {'cuda'} else {'cpu'}
$baseUrl=([string]$settings.AnswerBaseUrl).Trim().TrimEnd('/')
if ($baseUrl.Contains("`r") -or $baseUrl.Contains("`n")) { throw 'Invalid generator base URL.' }
$environment=@(
    "COMPOSE_PROJECT_NAME=docmind-$($manifest.variant)","DOCMIND_VARIANT=$($manifest.variant)","DOCMIND_DEVICE=$device",
    "DOCMIND_WEB_PORT=$([int]$settings.WebPort)","DOCMIND_API_PORT=$([int]$settings.ApiPort)",
    "MYSQL_PASSWORD=$(New-SecretHex)","ELASTIC_PASSWORD=$(New-SecretHex)","REDIS_PASSWORD=$(New-SecretHex)","MINIO_PASSWORD=$(New-SecretHex)",
    "DOCMIND_ANSWER_BASE_URL=$baseUrl"
)
$logical=@($settings.Sources | ForEach-Object { @{source_id=[string]$_.source_id;display_name=[string]$_.display_name} })
[IO.File]::WriteAllText((Join-Path $local 'installation/sources.json'),(ConvertTo-Json -InputObject $logical -Depth 5),$utf8)
if (@($settings.Sources).Count -gt 0) {
    $worker=@{
        worker_id='docmind-runtime-host';api_base_uri="http://127.0.0.1:$([int]$settings.ApiPort)";key_id='docmind-runtime-host';
        shared_secret_file=(Join-Path $local 'secrets/worker-key');executable_path=[string]$settings.UEncryptorPath;
        executable_sha256=(Get-FileHash -LiteralPath ([string]$settings.UEncryptorPath) -Algorithm SHA256).Hash.ToLowerInvariant();
        executable_signer_thumbprint=$signature.SignerCertificate.Thumbprint;work_root=$work;
        cleanup_receipt_root=(Join-Path $local 'host/cleanup-receipts');decrypt_control_root=(Join-Path $local 'host/decrypt-control');
        sync_state_root=(Join-Path $local 'host/sync-state');claim_lease_seconds=1800;job_timeout_seconds=120;
        require_bitlocker=$true;backup_exclusion_acknowledged=$true;preview_enabled=$true;
        sources=@($settings.Sources | ForEach-Object { @{source_id=[string]$_.source_id;root=[IO.Path]::GetFullPath([string]$_.root)} })
    }
    $hostConfig=Join-Path $local 'host-worker.private.json'
    [IO.File]::WriteAllText($hostConfig,($worker | ConvertTo-Json -Depth 8),$utf8)
    $fileAcl=Get-Acl -LiteralPath $hostConfig
    $fileAcl.SetAccessRuleProtection($true,$false)
    foreach ($rule in @($fileAcl.Access)) { [void]$fileAcl.RemoveAccessRuleSpecific($rule) }
    foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18')) {
        $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))
    }
    Set-Acl -LiteralPath $hostConfig -AclObject $fileAcl
}
[IO.File]::WriteAllLines((Join-Path $local 'runtime.env'),$environment,$utf8)
Write-Output 'DOCMIND initialized with fresh keys. Existing server data was not imported.'
