[CmdletBinding()]
param(
    [string]$Endpoint = 'https://api.jina.ai/v1/rerank',
    [string]$Model = 'jina-reranker-v3.5',
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30,
    [ValidateRange(-1, 2)]
    [int]$ExpectedTopIndex = 0,
    [switch]$PromptForApiKey
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Model -ne 'jina-reranker-v3.5') {
    throw 'Only jina-reranker-v3.5 is accepted by this validation.'
}

$apiKey = $env:JINA_API_KEY
$secureKey = $null
$keyPointer = [IntPtr]::Zero

try {
    if ([string]::IsNullOrWhiteSpace($apiKey) -and $PromptForApiKey) {
        $secureKey = Read-Host -AsSecureString 'Jina API key'
        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    }

    if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey.Length -lt 12) {
        throw 'JINA_API_KEY is missing or too short. Set it in the process environment or use -PromptForApiKey.'
    }

    $documents = @(
        'Temporary plaintext must be deleted immediately after parsing and indexing.',
        'A database index improves lookup speed for product identifiers.',
        'The weather forecast says that rain is likely tomorrow.'
    )
    $payload = @{
        model = $Model
        query = 'When must temporary plaintext be deleted?'
        documents = $documents
        top_n = $documents.Count
        return_documents = $false
    } | ConvertTo-Json -Depth 4 -Compress

    $headers = @{
        Authorization = "Bearer $apiKey"
        'Content-Type' = 'application/json'
    }
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-RestMethod `
        -Uri $Endpoint `
        -Method Post `
        -Headers $headers `
        -Body $payload `
        -TimeoutSec $TimeoutSeconds
    $stopwatch.Stop()

    $results = @($response.results)
    if ($results.Count -ne $documents.Count) {
        throw "Jina returned $($results.Count) results for $($documents.Count) documents."
    }

    $seen = [Collections.Generic.HashSet[int]]::new()
    foreach ($result in $results) {
        $index = [int]$result.index
        $score = [double]$result.relevance_score
        if ($index -lt 0 -or $index -ge $documents.Count -or -not $seen.Add($index)) {
            throw "Jina returned an invalid or duplicate result index: $index"
        }
        if ([double]::IsNaN($score) -or [double]::IsInfinity($score)) {
            throw "Jina returned a non-finite relevance score for index $index."
        }
    }

    $topIndex = [int]$results[0].index
    if ($ExpectedTopIndex -ge 0 -and $topIndex -ne $ExpectedTopIndex) {
        throw "Jina live quality control failed: expected top index $ExpectedTopIndex, received $topIndex."
    }

    $usageTokens = $null
    if ($null -ne $response.usage -and $null -ne $response.usage.total_tokens) {
        $usageTokens = [int]$response.usage.total_tokens
    }

    [ordered]@{
        schema_version = 1
        checked_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        model = $Model
        endpoint_host = ([Uri]$Endpoint).DnsSafeHost
        duration_ms = $stopwatch.ElapsedMilliseconds
        result_count = $results.Count
        top_index = $topIndex
        expected_top_index = $ExpectedTopIndex
        response_shape_valid = $true
        usage_total_tokens = $usageTokens
        api_key_logged = $false
        prompt_or_documents_logged = $false
        passed = $true
    } | ConvertTo-Json
}
finally {
    $apiKey = $null
    if ($null -ne $secureKey) {
        $secureKey.Dispose()
    }
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
}
