<#
.SYNOPSIS
Bind the current DocMind shared workspace and its tenant default to the
already registered Cloudflare BGE-M3 model. Local is an explicit rollback.
.DESCRIPTION
Registration is a separate prerequisite. This script never handles credentials,
restarts services, reindexes documents, or changes source watchers.
#>
[CmdletBinding()]
param(
    [ValidateSet('Cloudflare', 'Local')][string]$Target = 'Cloudflare',
    [switch]$VerifyOnly,
    [string]$ApiBase = 'http://127.0.0.1:18080/api/v1',
    [string]$ExpectedDatasetId = '',
    [string]$ExpectedTenantId = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$uri = $null
if (-not [uri]::TryCreate($ApiBase, [UriKind]::Absolute, [ref]$uri) -or
    $uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1', 'localhost') -or
    $uri.AbsolutePath.TrimEnd('/') -ne '/api/v1' -or $uri.UserInfo -or
    $uri.Query -or $uri.Fragment) {
    throw 'ApiBase must be the local HTTP DocMind /api/v1 endpoint.'
}
$ApiBase = $ApiBase.TrimEnd('/')
$idPattern = '^[0-9a-f]{32}$'
if ($VerifyOnly -and $Target -ne 'Cloudflare') { throw 'VerifyOnly requires the Cloudflare target.' }
function Invoke-DocMindApi($Method, $Path, $Body) {
    try {
        $params = @{Method=$Method;Uri="$ApiBase$Path";WebSession=$script:modelSession;NoProxy=$true;TimeoutSec=90}
        if ($null -ne $Body) {
            $params.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8 -Compress))
            $params.ContentType = 'application/json'
        }
        $response = Invoke-RestMethod @params
        if ($null -eq $response -or $response.code -ne 0) { throw 'Application rejected the request.' }
        return $response.data
    } catch { throw "Model binding operation failed: $Method $Path (response suppressed)." }
}
try {
    $sessionResponse = Invoke-RestMethod -Method Post -Uri "$ApiBase/docmind/shared-session" -SessionVariable modelSession -NoProxy -TimeoutSec 90
    if ($sessionResponse.code -ne 0 -or -not $sessionResponse.data.ready) { throw 'Shared session unavailable.' }
} catch { throw 'Shared workspace session failed (response suppressed).' }

# /docmind/folders is read-only and names the workspace currently served by DocMind.
$workspace = Invoke-DocMindApi Get '/docmind/folders' $null
$datasetId = [string]$workspace.dataset_id
if ($datasetId -cnotmatch $idPattern -or ($ExpectedDatasetId -and $datasetId -cne $ExpectedDatasetId)) {
    throw 'Shared workspace dataset is missing or differs from the expected dataset.'
}
$user = Invoke-DocMindApi Get '/users/me' $null
$before = Invoke-DocMindApi Get "/datasets/$datasetId" $null
$tenantId = [string]$before.tenant_id
if ($tenantId -cnotmatch $idPattern -or $tenantId -cne [string]$user.id -or
    ($ExpectedTenantId -and $tenantId -cne $ExpectedTenantId) -or
    [string]$before.id -cne $datasetId) {
    throw 'Shared workspace tenant or dataset identity differs from the authenticated owner.'
}

$models = Invoke-DocMindApi Get '/models?type=embedding' $null
$cloudModels = @($models | Where-Object {
    $_.tenant_id -ceq $tenantId -and $_.name -ceq '@cf/baai/bge-m3' -and
    $_.provider_name -ceq 'OpenAI-API-Compatible' -and $_.instance_name -ceq 'cloudflare' -and
    $_.model_id -cmatch $idPattern -and $_.model_type -contains 'embedding'
})
if ($cloudModels.Count -ne 1) { throw 'Expected exactly one registered Cloudflare BGE-M3 embedding model for this tenant.' }
$cloudId = [string]$cloudModels[0].model_id
$cloudName = '@cf/baai/bge-m3@cloudflare@OpenAI-API-Compatible'
$local = 'BAAI/bge-m3@Builtin'
$localDefault = 'BAAI/bge-m3@default@Builtin'
if ($before.embedding_model -notin @($cloudId, $cloudName, $local, $localDefault, 'BAAI/bge-m3')) {
    throw 'Unexpected dataset embedding model; no changes made.'
}
$defaults = Invoke-DocMindApi Get '/models/default' $null
$embeddingDefaults = @($defaults.models | Where-Object { $_.model_type -ceq 'embedding' })
if ($embeddingDefaults.Count -ne 1) { throw 'Expected one readable tenant embedding default.' }
$currentDefault = $embeddingDefaults[0]
$defaultIsCloud = $currentDefault.model_id -ceq $cloudId -and
    $currentDefault.model_provider -ceq 'OpenAI-API-Compatible' -and
    $currentDefault.model_instance -ceq 'cloudflare' -and
    $currentDefault.model_name -ceq '@cf/baai/bge-m3'
$defaultIsLocal = $currentDefault.model_provider -ceq 'Builtin' -and
    $currentDefault.model_instance -ceq 'default' -and
    $currentDefault.model_name -ceq 'BAAI/bge-m3'
if (-not ($defaultIsCloud -or $defaultIsLocal)) { throw 'Unexpected tenant embedding default; no changes made.' }

$newBinding = if ($Target -eq 'Cloudflare') { $cloudId } else { $local }
$newDefault = if ($Target -eq 'Cloudflare') {
    @{model_type='embedding';model_id=$cloudId}
} else {
    @{model_type='embedding';model_provider='Builtin';model_instance='default';model_name='BAAI/bge-m3'}
}
$alreadyBound = if ($Target -eq 'Cloudflare') {
    $before.embedding_model -cin @($cloudId, $cloudName) -and $before.tenant_embd_id -ceq $cloudId -and $defaultIsCloud
} else {
    $before.embedding_model -in @($local, $localDefault) -and $defaultIsLocal
}
if ($VerifyOnly -and -not $alreadyBound) { throw 'Cloudflare binding is not complete; keep ingestion stopped.' }
if (-not $alreadyBound -and -not $VerifyOnly) {
    # The dataset controls ingestion; the tenant default controls new datasets.
    $null = Invoke-DocMindApi Put "/datasets/$datasetId" @{embedding_model=$newBinding}
    try {
        $null = Invoke-DocMindApi Patch '/models/default' $newDefault
    } catch {
        try {
            $null = Invoke-DocMindApi Put "/datasets/$datasetId" @{embedding_model=$before.embedding_model}
        } catch { throw 'Default update failed and dataset rollback failed; inspect bindings before ingestion.' }
        throw 'Default update failed; dataset rollback was requested. Verify both bindings before ingestion.'
    }
}
$after = Invoke-DocMindApi Get "/datasets/$datasetId" $null
$afterDefaults = Invoke-DocMindApi Get '/models/default' $null
$afterEmbeddingDefaults = @($afterDefaults.models | Where-Object { $_.model_type -ceq 'embedding' })
$defaultMatches = if ($Target -eq 'Cloudflare') {
    $afterEmbeddingDefaults.Count -eq 1 -and $afterEmbeddingDefaults[0].model_id -ceq $cloudId
} else {
    $afterEmbeddingDefaults.Count -eq 1 -and $afterEmbeddingDefaults[0].model_provider -ceq 'Builtin' -and
    $afterEmbeddingDefaults[0].model_name -ceq 'BAAI/bge-m3'
}
$datasetMatches = if ($Target -eq 'Cloudflare') {
    $after.embedding_model -cin @($cloudId, $cloudName) -and $after.tenant_embd_id -ceq $cloudId
} else {
    $after.embedding_model -in @($local, $localDefault)
}
if (-not ($datasetMatches -and $defaultMatches -and $after.tenant_id -ceq $tenantId)) {
    throw 'Read-back mismatch; inspect both bindings before ingestion.'
}
[pscustomobject]@{target=$Target;dataset_id=$datasetId;tenant_id=$tenantId;embedding_model=$after.embedding_model;tenant_embd_id=$after.tenant_embd_id;already_bound=$alreadyBound;verify_only=[bool]$VerifyOnly;restart_performed=$false;reindex_performed=$false} | ConvertTo-Json
