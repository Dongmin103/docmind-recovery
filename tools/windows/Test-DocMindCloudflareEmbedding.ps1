$ErrorActionPreference = 'Stop'
$switchScript = Join-Path $PSScriptRoot 'Set-DocMindCloudflareEmbedding.ps1'
$tenant = '1f3dddbcb65011f19a7781e282c0c3b6'
$model = '11111111111111111111111111111111'
$local = 'BAAI/bge-m3@Builtin'
$global:DocMindCloudflareTestState = @{checks=0;tenant=$tenant;model=$model}
function Invoke-RestMethod {
    param($Method, $Uri, $WebSession, $TimeoutSec, $Body, $ContentType, $SessionVariable, [switch]$NoProxy)
    $path = ([uri]$Uri).AbsolutePath
    if ($path.EndsWith('/shared-session')) { return @{code=0} }
    $state=$global:DocMindCloudflareTestState
    if ($path.EndsWith('/models')) { return @{code=0;data=@(@{tenant_id=$state.tenant;model_id=$state.model;name='@cf/baai/bge-m3';provider_name='OpenAI-API-Compatible';instance_name='cloudflare'})} }
    if ($Method -eq 'Get') { return @{code=0;data=@{tenant_id=$state.owner;embedding_model=$state.binding;tenant_embd_id=$state.modelBinding}} }
    $parsed = [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
    if ($Method -eq 'Put') {
        $state.writes++
        $state.binding=$parsed.embedding_model
        $state.modelBinding=if ($state.binding -eq $state.model) { $state.model } else { $null }
    }
    if ($Method -eq 'Patch' -and $state.failDefault) { return @{code=500} }
    return @{code=0;data=@{}}
}
function Reset-Test($binding) {
    $state=$global:DocMindCloudflareTestState
    $state.binding=$binding; $state.owner=$tenant; $state.writes=0; $state.failDefault=$false; $state.modelBinding=$null
}
function Assert-Test($condition, $message) {
    if (-not $condition) { throw $message }
    $global:DocMindCloudflareTestState.checks++
}
Reset-Test $local
$null = & $switchScript -Target Cloudflare
Assert-Test ($global:DocMindCloudflareTestState.binding -eq $model) 'Cloudflare must use the registered model ID.'
$null = & $switchScript -Target Local
Assert-Test ($global:DocMindCloudflareTestState.binding -eq $local) 'Local rollback binding must be restored.'
Reset-Test $local
$global:DocMindCloudflareTestState.failDefault=$true
$failed=$false
try { $null = & $switchScript } catch { $failed=$true }
Assert-Test ($failed -and $global:DocMindCloudflareTestState.binding -eq $local -and $global:DocMindCloudflareTestState.writes -eq 2) 'Failed default update must compensate dataset update.'
Reset-Test 'unrelated-model'
$failed=$false
try { $null = & $switchScript } catch { $failed=$true }
Assert-Test ($failed -and $global:DocMindCloudflareTestState.writes -eq 0) 'Unknown existing bindings must not be changed.'
Reset-Test $local
$global:DocMindCloudflareTestState.owner='another-tenant'
$failed=$false
try { $null = & $switchScript } catch { $failed=$true }
Assert-Test ($failed -and $global:DocMindCloudflareTestState.writes -eq 0) 'Tenant boundary must be enforced.'
Reset-Test $local
$failed=$false
try { $null = & $switchScript -ApiBase 'https://example.com/api/v1' } catch { $failed=$true }
Assert-Test ($failed -and $global:DocMindCloudflareTestState.writes -eq 0) 'Nonlocal deployments must be rejected.'
Write-Output "$($global:DocMindCloudflareTestState.checks) Cloudflare binding checks passed (mock API; no network or DB writes)."
Remove-Variable -Name DocMindCloudflareTestState -Scope Global
