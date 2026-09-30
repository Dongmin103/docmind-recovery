$ErrorActionPreference = 'Stop'
$switchScript = Join-Path $PSScriptRoot 'Set-DocMindCloudflareEmbedding.ps1'
$tenant = '87820e6cbc7111f1986bf7c75f5ea8fe'
$dataset = 'bcdc4cea49254ea57a76d9ae61ade9d5'
$model = '11111111111111111111111111111111'
$local = 'BAAI/bge-m3'
$global:DocMindCloudflareTestState = @{checks=0}

function Reset-Test {
    $state = $global:DocMindCloudflareTestState
    $state.tenant=$tenant; $state.dataset=$dataset; $state.user=$tenant
    $state.model=$model; $state.modelsPresent=$true; $state.binding=$local
    $state.tenantModel=''; $state.default='local'; $state.writes=0
    $state.failDefault=$false; $state.apiCalls=0
}
function Assert-Test($condition, $message) {
    if (-not $condition) { throw $message }
    $global:DocMindCloudflareTestState.checks++
}
function Invoke-RestMethod {
    param($Method, $Uri, $WebSession, $TimeoutSec, $Body, $ContentType, $SessionVariable, [switch]$NoProxy)
    $state=$global:DocMindCloudflareTestState
    $state.apiCalls++
    $path=([uri]$Uri).AbsolutePath
    if ($path.EndsWith('/shared-session')) { return @{code=0;data=@{ready=$true}} }
    if ($path.EndsWith('/docmind/folders')) { return @{code=0;data=@{dataset_id=$state.dataset}} }
    if ($path.EndsWith('/users/me')) { return @{code=0;data=@{id=$state.user}} }
    if ($path.EndsWith('/models') -and $Method -eq 'Get') {
        $models = if ($state.modelsPresent) {
            @(@{tenant_id=$state.tenant;model_id=$state.model;name='@cf/baai/bge-m3';provider_name='OpenAI-API-Compatible';instance_name='cloudflare';model_type=@('embedding')})
        } else { @() }
        return @{code=0;data=$models}
    }
    if ($path.EndsWith('/models/default')) {
        if ($Method -eq 'Get') {
            $default = if ($state.default -eq 'cloud') {
                @{model_type='embedding';model_id=$state.model;model_provider='OpenAI-API-Compatible';model_instance='cloudflare';model_name='@cf/baai/bge-m3'}
            } else {
                @{model_type='embedding';model_id='';model_provider='Builtin';model_instance='default';model_name='BAAI/bge-m3'}
            }
            return @{code=0;data=@{models=@($default)}}
        }
        if ($state.failDefault) { return @{code=500} }
        $state.writes++
        $parsed = [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
        $state.default = if ($parsed.model_id -eq $state.model) { 'cloud' } else { 'local' }
        return @{code=0;data=@{}}
    }
    if ($path.EndsWith("/datasets/$($state.dataset)")) {
        if ($Method -eq 'Get') {
            return @{code=0;data=@{id=$state.dataset;tenant_id=$state.tenant;embedding_model=$state.binding;tenant_embd_id=$state.tenantModel}}
        }
        $state.writes++
        $parsed = [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
        if ($parsed.embedding_model -eq $state.model) {
            # The live GET endpoint returns the registered model ID, not its composite name.
            $state.binding=$state.model; $state.tenantModel=$state.model
        } else {
            $state.binding=$parsed.embedding_model; $state.tenantModel=''
        }
        return @{code=0;data=@{}}
    }
    throw "Unexpected mock request: $Method $path"
}
function Must-Fail($action, $message) {
    $failed=$false
    try { $null = & $action } catch { $failed=$true }
    Assert-Test $failed $message
}

Reset-Test
$result = & $switchScript -Target Cloudflare -ExpectedDatasetId $dataset -ExpectedTenantId $tenant | ConvertFrom-Json
Assert-Test ($result.dataset_id -eq $dataset -and $result.tenant_id -eq $tenant) 'Current workspace identity must be discovered.'
Assert-Test ($global:DocMindCloudflareTestState.binding -eq $model -and $global:DocMindCloudflareTestState.default -eq 'cloud') 'Dataset and tenant must both use Cloudflare.'
$global:DocMindCloudflareTestState.writes=0
$null = & $switchScript -VerifyOnly
Assert-Test ($global:DocMindCloudflareTestState.writes -eq 0) 'VerifyOnly must be read-only when Cloudflare is bound.'
$result = & $switchScript -Target Cloudflare | ConvertFrom-Json
Assert-Test ($result.already_bound -and $global:DocMindCloudflareTestState.writes -eq 0) 'An existing model ID binding must pass repeat apply without writes.'
$null = & $switchScript -Target Local
Assert-Test ($global:DocMindCloudflareTestState.default -eq 'local' -and $global:DocMindCloudflareTestState.binding -eq 'BAAI/bge-m3@Builtin') 'Local rollback must restore both bindings.'

Reset-Test
Must-Fail { & $switchScript -VerifyOnly } 'VerifyOnly must reject the local default.'
Assert-Test ($global:DocMindCloudflareTestState.writes -eq 0) 'A failed gate must not write.'
$global:DocMindCloudflareTestState.failDefault=$true
Must-Fail { & $switchScript } 'Default failure must fail the cutover.'
Assert-Test ($global:DocMindCloudflareTestState.binding -eq $local -and $global:DocMindCloudflareTestState.writes -eq 2) 'Default failure must request dataset rollback.'

Reset-Test
$global:DocMindCloudflareTestState.modelsPresent=$false
Must-Fail { & $switchScript } 'Missing registration must fail.'
Assert-Test ($global:DocMindCloudflareTestState.writes -eq 0) 'Missing registration must not write.'
Reset-Test
$global:DocMindCloudflareTestState.user='22222222222222222222222222222222'
Must-Fail { & $switchScript } 'Owner mismatch must fail.'
Assert-Test ($global:DocMindCloudflareTestState.writes -eq 0) 'Owner mismatch must not write.'
Reset-Test
Must-Fail { & $switchScript -ExpectedDatasetId '33333333333333333333333333333333' } 'Expected dataset mismatch must fail.'
Assert-Test ($global:DocMindCloudflareTestState.writes -eq 0) 'Dataset mismatch must not write.'
Reset-Test
$global:DocMindCloudflareTestState.binding='unknown-model'
Must-Fail { & $switchScript } 'Unknown existing binding must fail.'
Assert-Test ($global:DocMindCloudflareTestState.writes -eq 0) 'Unknown binding must not write.'
Reset-Test
$global:DocMindCloudflareTestState.apiCalls=0
Must-Fail { & $switchScript -ApiBase 'https://example.com/api/v1' } 'Nonlocal endpoint must fail.'
Assert-Test ($global:DocMindCloudflareTestState.apiCalls -eq 0) 'Rejected endpoint must not be called.'
Write-Output "$($global:DocMindCloudflareTestState.checks) Cloudflare binding checks passed (mock API; no network or DB writes)."
Remove-Variable -Name DocMindCloudflareTestState -Scope Global
