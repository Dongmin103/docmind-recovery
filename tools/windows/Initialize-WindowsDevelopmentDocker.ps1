[CmdletBinding()]
param(
    [string]$OutputPath,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $OutputPath) {
    $OutputPath = Join-Path $repoRoot '.local/docker/windows-dev.env'
}
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local'))
if (-not $resolvedOutput.StartsWith($localRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Development environment files must stay below $localRoot"
}
if ((Test-Path -LiteralPath $resolvedOutput) -and -not $Force) {
    throw "Refusing to overwrite $resolvedOutput. Re-run with -Force to rotate local development credentials."
}

function New-RandomHex([int]$Bytes) {
    $buffer = [byte[]]::new($Bytes)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    } finally {
        $generator.Dispose()
    }
    return ([BitConverter]::ToString($buffer) -replace '-', '').ToLowerInvariant()
}

$mysqlPassword = New-RandomHex 24
$elasticPassword = New-RandomHex 24
$redisPassword = New-RandomHex 24
$minioUser = 'docminddev' + (New-RandomHex 6)
$minioPassword = New-RandomHex 24

$content = @"
STACK_VERSION=8.11.3
RAGFLOW_IMAGE=docmind-ragflow:windows-dev
DOC_ENGINE=elasticsearch
DEVICE=cpu
TZ=Asia/Seoul
DOCMIND_DEV_SECURITY_MODE=isolated-synthetic-only
DOCMIND_DEV_DATA_CLASS=synthetic-only
DOCMIND_DEV_ALLOW_PLAINTEXT_LOOPBACK=1
DOCMIND_DEV_EXTERNAL_API_POLICY=https-only
DOCMIND_HOST_WORKER_KEY_ID=windows-host-1
DOCMIND_HOST_WORKER_SECRET_FILE=../../.local/docker/host-worker-hmac.key
DOCMIND_DEV_WEB_PORT=18080
DOCMIND_DEV_API_PORT=19380
DOCMIND_DEV_ADMIN_PORT=19381
DOCMIND_DEV_MYSQL_PORT=13306
DOCMIND_DEV_ES_PORT=19200
DOCMIND_DEV_REDIS_PORT=16379
DOCMIND_DEV_MINIO_PORT=19000
DOCMIND_DEV_MINIO_CONSOLE_PORT=19001
DOCMIND_DEV_BGE_PORT=16380
MYSQL_PASSWORD=$mysqlPassword
MYSQL_DBNAME=rag_flow_dev
MYSQL_HOST=mysql
MYSQL_PORT=3306
ELASTIC_PASSWORD=$elasticPassword
ES_HOST=es01
ES_PORT=9200
REDIS_PASSWORD=$redisPassword
REDIS_HOST=redis
REDIS_PORT=6379
MINIO_USER=$minioUser
MINIO_PASSWORD=$minioPassword
MINIO_HOST=minio
MINIO_PORT=9000
STORAGE_IMPL=MINIO
TEI_HOST=bge-m3-cpu
BGE_M3_REVISION=5617a9f61b028005a4858fdac845db406aefb181
DOCMIND_RERANK_ID=jina-reranker-v3.5@jina@Jina
JINA_API_KEY=
DOCMIND_GENERATOR_MODEL=
DASHSCOPE_API_KEY=
PARSER_PLATFORM_ENABLED=0
PARSER_PLATFORM_INTEGRATION_READY=0
TE_RUN_MODE=0
ES_MEMORY_LIMIT=2g
MYSQL_MEMORY_LIMIT=1g
MINIO_MEMORY_LIMIT=1g
REDIS_MEMORY_LIMIT=256m
RAGFLOW_MEMORY_LIMIT=4g
BGE_M3_MEMORY_LIMIT=4g
BGE_M3_MEMORY_SWAP_LIMIT=6g
BGE_M3_CPUS=4
"@

[IO.Directory]::CreateDirectory((Split-Path -Parent $resolvedOutput)) | Out-Null
[IO.File]::WriteAllText($resolvedOutput, $content, [Text.UTF8Encoding]::new($false))
& (Join-Path $PSScriptRoot 'Initialize-DocMindHostWorkerSecret.ps1') -EnvironmentPath $resolvedOutput -RotateSecret:$Force
Write-Output "Created ignored development environment: $resolvedOutput"
Write-Output 'No external API keys were added. Set JINA_API_KEY and the answer-model credentials locally when available.'
