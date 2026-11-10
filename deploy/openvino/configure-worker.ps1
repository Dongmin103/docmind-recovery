[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$OutputDirectory,
 [Parameter(Mandatory=$true)][string]$WorkerKeyFile,
 [Parameter(Mandatory=$true)][string]$UEncryptorPath,
 [Parameter(Mandatory=$true)][string]$SourceRoot,
 [Parameter(Mandatory=$true)][string]$SourceId,
 [Parameter(Mandatory=$true)][string]$ApiBaseUri,
 [Parameter(Mandatory=$true)][string]$ApprovedExeSHA256,
 [Parameter(Mandatory=$true)][string]$ApprovedSignerThumbprint,
 [switch]$BackupExclusionAcknowledged
)
$ErrorActionPreference='Stop'
$target=[IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $target) { throw 'Choose a new directory; existing worker configuration is preserved.' }
$uri=[Uri]$ApiBaseUri
if (-not $uri.IsLoopback -or $uri.Scheme -notin @('http','https')) { throw 'Use an approved loopback connection/tunnel to the Ubuntu API.' }
if ($SourceId -cnotmatch '^[a-z0-9][a-z0-9-]{0,63}$') { throw 'Invalid source ID.' }
foreach ($path in @($WorkerKeyFile,$UEncryptorPath)) { if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Required file unavailable.' } }
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) { throw 'Source directory unavailable.' }
$src=[IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
if ($target -eq $src -or $target.StartsWith($src+'\',[StringComparison]::OrdinalIgnoreCase) -or $src.StartsWith($target.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Worker runtime and source must not overlap.' }
$signature=Get-AuthenticodeSignature -LiteralPath $UEncryptorPath
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Thumbprint -ne $ApprovedSignerThumbprint -or (Get-FileHash -LiteralPath $UEncryptorPath -Algorithm SHA256).Hash -ne $ApprovedExeSHA256) { throw 'Installed decryptor differs from approved signature/hash.' }
if (-not $BackupExclusionAcknowledged) { throw 'Confirm backup exclusion for the dedicated temporary plaintext directory.' }
[void](New-Item -ItemType Directory -Path $target)
$acl=Get-Acl -LiteralPath $target
$acl.SetAccessRuleProtection($true,$false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18')) {
 $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
Set-Acl -LiteralPath $target -AclObject $acl
Copy-Item -LiteralPath $WorkerKeyFile -Destination (Join-Path $target 'worker-key')
$c=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'tools/windows/docmind-host-worker.config.example.json') -Raw | ConvertFrom-Json
$c.worker_id='openvino-host'; $c.key_id='docmind-runtime-host'; $c.api_base_uri=$ApiBaseUri
$c.shared_secret_file=Join-Path $target 'worker-key'
$c.executable_path=[IO.Path]::GetFullPath($UEncryptorPath); $c.executable_sha256=$ApprovedExeSHA256; $c.executable_signer_thumbprint=$ApprovedSignerThumbprint
foreach ($pair in @(@('work_root','plaintext'),@('cleanup_receipt_root','receipts'),@('decrypt_control_root','decrypt-control'),@('sync_state_root','sync-state'))) { $c.($pair[0])=Join-Path $target $pair[1] }
$c.sources=@(@{source_id=$SourceId;root=$src}); $c.backup_exclusion_acknowledged=$true; $c.preview_enabled=$false
$config=Join-Path $target 'worker.json'
[IO.File]::WriteAllText($config,($c | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
Write-Output ('Configuration created: '+$config)
Write-Output 'Register the same logical source in the engine before starting the watcher and worker. BitLocker validation remains enabled.'
