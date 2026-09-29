<#
.SYNOPSIS
Switch this DocMind shared workspace between its registered Cloudflare BGE-M3
instance and the retained local BGE-M3 service. Does not restart containers,
reindex documents, change reranking, or alter source watcher configuration.
Cloudflare credentials must already be registered using the provider API.
#>
[CmdletBinding()]
param(
    [ValidateSet('Cloudflare', 'Local')][string]$Target = 'Cloudflare',
    [string]$ApiBase = 'http://127.0.0.1:18080/api/v1',
    [string]$DatasetId = '5ba70756384649c20b8ef2c4cb214d88',
    [string]$TenantId = '1f3dddbcb65011f19a7781e282c0c3b6'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (([uri]$ApiBase).Host -notin @('127.0.0.1', 'localhost')) { throw 'Only the local deployment is supported.' }
$ApiBase = $ApiBase.TrimEnd('/')
function Invoke-DocMindApi($Method, $Path, $Body) {
    try {
        $params = @{Method=$Method;Uri="$ApiBase$Path";WebSession=$script:modelSession;NoProxy=$true;TimeoutSec=90}
        if ($null -ne $Body) {
            $params.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8 -Compress))
            $params.ContentType = 'application/json'
        }
        $response = Invoke-RestMethod @params
        if ($response.code -ne 0) { throw 'Application rejected the request.' }
        return $response.data
    } catch { throw "Model binding operation failed: $Method $Path (response suppressed)." }
}
$null = Invoke-RestMethod -Method Post -Uri "$ApiBase/docmind/shared-session" -SessionVariable modelSession -NoProxy
$before = Invoke-DocMindApi Get "/datasets/$DatasetId" $null
if ($before.tenant_id -ne $TenantId) { throw 'Unexpected dataset tenant; no changes made.' }
$cloud = '@cf/baai/bge-m3@cloudflare@OpenAI-API-Compatible'
$models = Invoke-DocMindApi Get '/models?model_type=embedding' $null
$cloudModels = @($models | Where-Object { $_.tenant_id -eq $TenantId -and $_.name -eq '@cf/baai/bge-m3' -and $_.provider_name -eq 'OpenAI-API-Compatible' -and $_.instance_name -eq 'cloudflare' })
if ($cloudModels.Count -ne 1) { throw 'Expected exactly one registered Cloudflare model.' }
$cloudId = $cloudModels[0].model_id
$local = 'BAAI/bge-m3@Builtin'
$localDefault = 'BAAI/bge-m3@default@Builtin'
if ($before.embedding_model -notin @($cloudId, $cloud, $local, $localDefault)) { throw 'Unexpected current model; review before switching.' }
$newBinding = if ($Target -eq 'Cloudflare') { $cloudId } else { $local }
$default = if ($Target -eq 'Cloudflare') {
    @{model_type='embedding';model_id=$cloudId}
} else {
    @{model_type='embedding';model_provider='Builtin';model_instance='default';model_name='BAAI/bge-m3'}
}
# Update the dataset first (the actual ingestion/search binding), then the
# tenant default for future datasets. Compensate if the second request fails.
$null = Invoke-DocMindApi Put "/datasets/$DatasetId" @{embedding_model=$newBinding}
try {
    $null = Invoke-DocMindApi Patch '/models/default' $default
} catch {
    $null = Invoke-DocMindApi Put "/datasets/$DatasetId" @{embedding_model=$before.embedding_model}
    throw
}
$after = Invoke-DocMindApi Get "/datasets/$DatasetId" $null
if ($after.embedding_model -ne $newBinding) { throw 'Read-back mismatch. Inspect before further changes.' }
[pscustomobject]@{target=$Target;dataset_id=$DatasetId;embedding_model=$after.embedding_model;tenant_embd_id=$after.tenant_embd_id;restart_performed=$false;reindex_performed=$false} | ConvertTo-Json
