param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$SourceId = 'dept-2-e2e',
    [string]$ClaimFormats = 'xlsx',
    [string]$ClaimJobId = '',
    [ValidateRange(1, 25)][int]$MaxJobs = 3,
    [ValidateRange(0, 10)][int]$MaxApiRestarts = 2,
    [ValidateRange(0.8, 8.0)][double]$MinHostFreeGiB = 1.5,
    [ValidateRange(1, 30)][int]$MaxPdfPages = 30,
    [switch]$AutoRecoverMemory,
    [switch]$CheckOnly,
    [string]$LogRoot = 'C:\DocMindDev\docmind\.local\uEncryptor2\backlog-runs'
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($SourceId -notmatch '^[A-Za-z0-9_-]{1,64}$') { throw 'SOURCE_ID_INVALID' }
$formats = @($ClaimFormats.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() })
if ($formats.Count -eq 0 -or @($formats | Where-Object { $_ -notin @('pdf', 'doc', 'docx', 'xlsx', 'pptx') }).Count -gt 0 -or
    @($formats | Select-Object -Unique).Count -ne $formats.Count) { throw 'CLAIM_FORMATS_INVALID' }
if ($formats -contains 'pdf' -and $formats.Count -ne 1) { throw 'PDF_PAGE_CAP_REQUIRES_PDF_ONLY' }
if ($formats -contains 'pdf' -and $SourceId -ne 'dept-2-e2e') { throw 'PDF_PAGE_CAP_SOURCE_INVALID' }
if ($ClaimJobId -and ($ClaimJobId -cnotmatch '^[0-9a-f]{32}$' -or $SourceId -ne 'dept-2-e2e' -or
    $formats.Count -ne 1 -or $formats[0] -ne 'pptx' -or $MaxJobs -ne 1)) {
    throw 'EXACT_PPTX_CLAIM_ARGUMENTS_INVALID'
}
$configFile = [IO.Path]::GetFullPath($ConfigPath)
$config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
$workRoot = [IO.Path]::GetFullPath([string]$config.work_root)
$receiptRoot = [IO.Path]::GetFullPath([string]$config.cleanup_receipt_root)
$apiContainer = 'docmind-windows-dev-ragflow-cpu-1'
$suryaContainer = 'docmind-windows-dev-surya-parser-cpu-1'
$mysqlContainer = 'docmind-windows-dev-mysql-1'
$apiUrl = 'http://127.0.0.1:18080'
$workerScript = Join-Path $PSScriptRoot 'Start-DocMindUEncryptorHostWorker.ps1'
$workerExecutable = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$runId = [Guid]::NewGuid().ToString('n')
$logDirectory = [IO.Path]::GetFullPath($LogRoot)
[IO.Directory]::CreateDirectory($logDirectory) | Out-Null
$jsonl = Join-Path $logDirectory 'supported-backlog.jsonl'
$markdown = Join-Path $logDirectory 'supported-backlog.md'
$mutex = [Threading.Mutex]::new($false, 'Global\DocMind-SupportedBacklog-SingleRun')
$acquired = $false

function Write-RunEvent {
    param([string]$Event, [hashtable]$Details = @{})
    $record = [ordered]@{ utc = [DateTimeOffset]::UtcNow.ToString('o'); run_id = $runId; event = $Event }
    foreach ($key in $Details.Keys) { $record[$key] = $Details[$key] }
    Add-Content -LiteralPath $jsonl -Value ($record | ConvertTo-Json -Compress -Depth 8) -Encoding utf8
    $detailText = (($Details.Keys | Sort-Object | ForEach-Object { "${_}=$($Details[$_])" }) -join ', ')
    Add-Content -LiteralPath $markdown -Value ("- {0} UTC — **{1}** {2}" -f $record.utc, $Event, $detailText) -Encoding utf8
    Write-Output ("{0}: {1}" -f $Event, $detailText)
}

function Invoke-Docker {
    param([string[]]$Arguments, [int]$TimeoutSeconds = 8)
    $result = & wsl.exe -d Ubuntu -- timeout "${TimeoutSeconds}s" docker @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw "DOCKER_COMMAND_FAILED: $($Arguments[0])" }
    return @($result)
}

function Invoke-DbLines {
    param([string]$Sql)
    # Windows PowerShell's native stdin writer emits a BOM before our raw bytes.
    # A leading blank line keeps that marker separate from the mysql command.
    $shell = "`nmysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -N rag_flow_dev <<'DOCMIND_SQL'`n$Sql`nDOCMIND_SQL`n"
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = 'wsl.exe'
    $start.Arguments = "-d Ubuntu -- timeout 8s docker exec -i $mysqlContainer sh"
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'DATABASE_PROCESS_START_FAILED' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($shell)
        $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(12000)) { $process.Kill(); throw 'DATABASE_QUERY_TIMEOUT' }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "DATABASE_QUERY_FAILED: $($stderr.Trim())" }
        return @([regex]::Split($stdout.TrimEnd("`r", "`n"), '\r?\n') | Where-Object { $_.Length -gt 0 })
    } finally { $process.Dispose() }
}

function Get-EligibleIds {
    param([bool]$OtherSource = $false)
    $sourcePredicate = if ($OtherSource) { "j.source_id <> '$SourceId'" } else { "j.source_id = '$SourceId'" }
    $formatsSql = ($formats | ForEach-Object { "'$_'" }) -join ','
    $sql = "SELECT j.id FROM docmind_ingestion_job j JOIN docmind_source_document d ON d.id=j.source_document_id JOIN docmind_source s ON s.id=j.source_id WHERE $sourcePredicate AND j.lifecycle_state='DISCOVERED' AND d.deleted_at IS NULL AND s.enabled=1 AND LOWER(SUBSTRING_INDEX(d.relative_path,'.',-1)) IN ($formatsSql) AND SUBSTRING_INDEX(d.relative_path,'/',-1) NOT LIKE '~`$%' ORDER BY j.create_time,j.id;"
    return @(Invoke-DbLines $sql)
}

function Assert-WorkerIdle {
    $task = Get-ScheduledTask -TaskName 'DocMind Host Worker'
    if ($task.State -ne 'Disabled' -or $task.Actions[0].Arguments -notmatch '(?i)(^|\s)-SkipRetries(\s|$)') { throw 'WORKER_TASK_NOT_SAFELY_DISABLED' }
    $workers = @(Get-CimInstance Win32_Process -Filter "name='powershell.exe'" | Where-Object {
        $_.CommandLine -like '*Start-DocMindUEncryptorHostWorker.ps1*' -and $_.CommandLine -notlike '*Get-CimInstance*'
    })
    if ($workers.Count -gt 0) { throw "OTHER_WORKER_PROCESS_RUNNING: $($workers[0].ProcessId)" }
}

function Assert-CleanupIdle {
    $active = @(Invoke-DbLines "SELECT COUNT(*) FROM docmind_ingestion_job WHERE lifecycle_state IN ('CLAIMED','DECRYPTING','DELIVERED','PARSING','INDEXING','CLEANUP','CLEANUP_FAILED') OR host_cleanup_state='PENDING';")
    if ($active.Count -ne 1 -or $active[0] -ne '0') { throw "ACTIVE_OR_CLEANUP_PENDING: $($active -join ',')" }
    if (@(Get-ChildItem -LiteralPath $workRoot -Recurse -File -Filter 'plaintext.*').Count -ne 0) { throw 'HOST_PLAINTEXT_PRESENT' }
    if (@(Get-ChildItem -LiteralPath $receiptRoot -File).Count -ne 0) { throw 'HOST_RECEIPT_PRESENT' }
}

function Assert-SuryaReady {
    $state = (Invoke-Docker -Arguments @('inspect', '--format', '{{.State.Status}} {{.State.Health.Status}} {{.State.OOMKilled}}', $suryaContainer)) -join ''
    if ($state -ne 'running healthy false') { throw "SURYA_NOT_READY: $state" }
    $check = (Invoke-Docker -Arguments @('exec', $apiContainer, 'curl', '-fsS', '--max-time', '3', 'http://surya-parser:8091/ready')) -join ''
    if (($check | ConvertFrom-Json).status -ne 'ready') { throw 'SURYA_ROUTE_NOT_READY' }
}

function Assert-SuryaInference {
    Assert-SuryaReady
    $events = @(Invoke-Docker -Arguments @('exec', $suryaContainer, 'cat', '/sys/fs/cgroup/memory.events'))
    if (($events -join "`n") -notmatch '(?m)^oom_kill 0$') { throw 'SURYA_OOM_EVENT_PRESENT' }
    $runIdForSmoke = [Guid]::NewGuid().ToString('N')
    $code = @'
import base64, hashlib, io, requests
from PIL import Image
buffer = io.BytesIO()
Image.new("RGB", (256, 256), "white").save(buffer, format="PNG")
media = buffer.getvalue()
payload = {"task_kind": "office_media_parse", "parse_run_id": "RUN_ID", "trace_id": "docmind-backlog-smoke", "media_id": "smoke", "media_hash": hashlib.sha256(media).hexdigest(), "source_locator": "#/smoke", "media_base64": base64.b64encode(media).decode("ascii")}
response = requests.post("http://surya-parser:8091/v1/parse-media", json=payload, timeout=30)
response.raise_for_status()
assert response.json().get("media_id") == "smoke"
print("SURYA_INFERENCE_OK")
'@.Replace('RUN_ID', $runIdForSmoke)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = 'wsl.exe'
    $start.Arguments = "-d Ubuntu -- timeout 35s docker exec -i $apiContainer /ragflow/.venv/bin/python -"
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'SURYA_INFERENCE_PROCESS_START_FAILED' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($code)
        $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(40000)) { $process.Kill(); throw 'SURYA_INFERENCE_TIMEOUT' }
        $stdout = $stdoutTask.GetAwaiter().GetResult().Trim()
        $stderr = $stderrTask.GetAwaiter().GetResult().Trim()
        if ($process.ExitCode -ne 0 -or $stdout -ne 'SURYA_INFERENCE_OK') { throw "SURYA_INFERENCE_FAILED: $stderr" }
    } finally { $process.Dispose() }
}

function Get-Memory {
    $raw = (Invoke-Docker -Arguments @('stats', '--no-stream', '--format', '{{.MemUsage}}', $apiContainer)) -join ''
    if ($raw -notmatch '^([0-9.]+)(GiB|MiB)') { throw 'API_MEMORY_UNKNOWN' }
    $apiGiB = [double]$Matches[1]
    if ($Matches[2] -eq 'MiB') { $apiGiB /= 1024 }
    $os = Get-CimInstance Win32_OperatingSystem
    return [pscustomobject]@{ api_gib = [math]::Round($apiGiB, 3); host_free_gib = [math]::Round($os.FreePhysicalMemory / 1MB, 3) }
}

function Assert-RetentionOff {
    $envLines = @(Invoke-Docker -Arguments @('inspect', '--format', '{{range .Config.Env}}{{println .}}{{end}}', $apiContainer))
    $flag = @($envLines | Where-Object { $_ -like 'DOCMIND_RETENTION_PURGE_ENABLED=*' })
    if ($flag.Count -gt 0 -and $flag[0] -notmatch '=(?i:false|0|off)$') { throw 'RETENTION_PURGE_ENABLED' }
}

function Wait-ApiReady {
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        try {
            $web = Invoke-WebRequest "$apiUrl/" -UseBasicParsing -TimeoutSec 5
            $status = 0
            try { Invoke-WebRequest "$apiUrl/api/v1/cloud-sync/host-worker/claim" -Method Post -Body '{}' -ContentType 'application/json' -UseBasicParsing -TimeoutSec 5 | Out-Null }
            catch { if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } }
            if ($web.StatusCode -eq 200 -and $status -eq 401) { return }
        } catch { }
        Start-Sleep -Seconds 5
    }
    throw 'API_NOT_READY_AFTER_RESTART'
}

function Restart-ApiSafely {
    Assert-CleanupIdle
    $before = (Invoke-Docker -Arguments @('exec', $apiContainer, 'sha256sum', '/ragflow/web/dist/index.html')) -join ''
    Invoke-Docker -Arguments @('restart', $apiContainer) -TimeoutSeconds 45 | Out-Null
    Wait-ApiReady
    $after = (Invoke-Docker -Arguments @('exec', $apiContainer, 'sha256sum', '/ragflow/web/dist/index.html')) -join ''
    if ($before -ne $after) { throw 'UI_DIST_CHANGED' }
    Assert-RetentionOff
    Assert-SuryaReady
    Write-RunEvent 'api_restarted' @{ ui_hash = ($after -split ' ')[0] }
}

function Reclaim-WslCacheSafely {
    Assert-WorkerIdle
    Assert-CleanupIdle
    $before = Get-Memory
    & wsl.exe -d Ubuntu -u root -- timeout 30s sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'WSL_CACHE_RECLAIM_FAILED' }
    $after = $before
    $polls = 0
    for ($poll = 1; $poll -le 3; $poll++) {
        Start-Sleep -Seconds 10
        $polls = $poll
        $after = Get-Memory
        if ($after.host_free_gib -ge $MinHostFreeGiB) { break }
    }
    Write-RunEvent 'wsl_cache_reclaimed' @{ before_host_free_gib = $before.host_free_gib; after_host_free_gib = $after.host_free_gib; api_gib = $after.api_gib; polls = $polls }
}

try {
    $acquired = $mutex.WaitOne(0)
    if (-not $acquired) { throw 'BACKLOG_RUN_ALREADY_ACTIVE' }
    Assert-WorkerIdle
    Assert-CleanupIdle
    Assert-RetentionOff
    Wait-ApiReady
    Assert-SuryaReady
    if ($AutoRecoverMemory -and -not $CheckOnly) { Assert-SuryaInference }
    $beforeIds = @(Get-EligibleIds)
    $otherIds = @(Get-EligibleIds -OtherSource $true)
    $memory = Get-Memory
    Write-RunEvent 'preflight' @{ source = $SourceId; formats = ($formats -join ','); claim_job_id = $ClaimJobId; eligible = $beforeIds.Count; other_source_eligible = $otherIds.Count; api_gib = $memory.api_gib; host_free_gib = $memory.host_free_gib; min_host_free_gib = $MinHostFreeGiB; max_pdf_pages = $(if ($formats -contains 'pdf') { $MaxPdfPages } else { $null }); auto_recover_memory = [bool]$AutoRecoverMemory; check_only = [bool]$CheckOnly }
    if ($CheckOnly) { return }
    $apiRestarts = 0
    for ($index = 1; $index -le $MaxJobs; $index++) {
        Assert-WorkerIdle
        Assert-CleanupIdle
        Assert-SuryaReady
        $beforeIds = @(Get-EligibleIds)
        if ($beforeIds.Count -eq 0) { Write-RunEvent 'backlog_empty'; break }
        if ($ClaimJobId -and $ClaimJobId -notin $beforeIds) { throw 'EXACT_PPTX_JOB_NOT_ELIGIBLE' }
        $memory = Get-Memory
        if ($AutoRecoverMemory -and ($memory.api_gib -ge 3.25 -or $memory.host_free_gib -lt $MinHostFreeGiB)) {
            if ($memory.api_gib -ge 3.25 -or $memory.host_free_gib -lt 0.8) {
                if ($apiRestarts -ge $MaxApiRestarts) { throw 'MEMORY_GUARD_RESTART_LIMIT' }
                Restart-ApiSafely
                $apiRestarts++
                $memory = Get-Memory
            }
            if ($memory.host_free_gib -lt $MinHostFreeGiB) {
                Reclaim-WslCacheSafely
                $memory = Get-Memory
            }
            Assert-CleanupIdle
            Assert-RetentionOff
            Assert-SuryaInference
        } elseif ($memory.api_gib -ge 3.45 -or $memory.host_free_gib -lt 0.8) {
            if ($apiRestarts -ge $MaxApiRestarts) { throw 'MEMORY_GUARD_RESTART_LIMIT' }
            Restart-ApiSafely
            $apiRestarts++
            $memory = Get-Memory
        }
        $apiLimit = if ($AutoRecoverMemory) { 3.25 } else { 3.6 }
        if ($memory.api_gib -ge $apiLimit -or $memory.host_free_gib -lt $MinHostFreeGiB) { throw "MEMORY_GUARD: api=$($memory.api_gib),host=$($memory.host_free_gib),min_host=$MinHostFreeGiB" }
        $stdout = Join-Path $logDirectory "$runId-$index.stdout.log"
        $stderr = Join-Path $logDirectory "$runId-$index.stderr.log"
        $arguments = "-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File `"$workerScript`" -ConfigPath `"$configFile`" -Once -SkipRetries -ClaimFormats `"$($formats -join ',')`" -ClaimSourceId `"$SourceId`""
        if ($formats -contains 'pdf') { $arguments += " -MaxPdfPages $MaxPdfPages" }
        if ($ClaimJobId) { $arguments += " -ClaimJobId $ClaimJobId" }
        $start = [Diagnostics.ProcessStartInfo]::new()
        $start.FileName = $workerExecutable
        $start.Arguments = $arguments
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.EnvironmentVariables['PSModulePath'] = (@(
            (Join-Path $env:USERPROFILE 'Documents\WindowsPowerShell\Modules'),
            (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'),
            (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules')
        ) -join [IO.Path]::PathSeparator)
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $start
        try {
            if (-not $process.Start()) { throw 'WORKER_START_FAILED' }
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
            Write-RunEvent 'worker_started' @{ sequence = $index; pid = $process.Id; eligible_before = $beforeIds.Count }
            if (-not $process.WaitForExit(2100000)) { throw "WORKER_STILL_RUNNING: pid=$($process.Id)" }
            [IO.File]::WriteAllText($stdout, $stdoutTask.GetAwaiter().GetResult(), [Text.UTF8Encoding]::new($false))
            [IO.File]::WriteAllText($stderr, $stderrTask.GetAwaiter().GetResult(), [Text.UTF8Encoding]::new($false))
            if ($process.ExitCode -ne 0) { throw "WORKER_EXIT_FAILED: pid=$($process.Id),code=$($process.ExitCode)" }
        } finally { $process.Dispose() }
        Assert-CleanupIdle
        $afterIds = @(Get-EligibleIds)
        $claimed = @($beforeIds | Where-Object { $_ -notin $afterIds })
        if ($claimed.Count -ne 1) { throw "CLAIM_RESULT_AMBIGUOUS: before=$($beforeIds.Count),after=$($afterIds.Count),missing=$($claimed.Count)" }
        $jobId = $claimed[0]
        if ($ClaimJobId -and $jobId -ne $ClaimJobId) { throw 'EXACT_PPTX_JOB_MISMATCH' }
        $row = @(Invoke-DbLines "SELECT lifecycle_state,cleanup_state,host_cleanup_state,COALESCE(error_code,''),attempt,fencing_token,CASE WHEN error_code='DOCMIND_PDF_PAGE_CAP_EXCEEDED' THEN COALESCE(error_message,'') ELSE '' END FROM docmind_ingestion_job WHERE id='$jobId';")
        if ($row.Count -ne 1) { throw 'JOB_STATUS_MISSING' }
        $parts = $row[0].Split("`t")
        if ($parts.Count -ne 7 -or $parts[0] -notin @('COMPLETE','FAILED') -or $parts[1] -ne 'COMPLETE' -or $parts[2] -ne 'COMPLETE') { throw "JOB_NOT_SAFELY_TERMINAL: $jobId" }
        $parserRow = @(Invoke-DbLines "SELECT p.id,p.lifecycle,p.warnings FROM parser_run p JOIN docmind_ingestion_job j ON j.parser_run_id=p.id WHERE j.id='$jobId';")
        $parserSource = 'linked_job_run'
        if ($parserRow.Count -eq 0) {
            # Failed jobs do not retain parser_run_id; disclose the fallback relationship.
            $parserRow = @(Invoke-DbLines "SELECT p.id,p.lifecycle,p.warnings FROM parser_run p JOIN docmind_ingestion_job j ON j.document_id=p.doc_id WHERE j.id='$jobId' AND p.create_date >= DATE_SUB(j.update_date, INTERVAL 30 MINUTE) ORDER BY p.create_time DESC LIMIT 1;")
            $parserSource = 'latest_document_run'
        }
        $parserRunId = ''
        $parserLifecycle = ''
        $parserWarnings = '[]'
        if ($parserRow.Count -eq 1) {
            $parserParts = $parserRow[0] -split "`t", 3
            $parserRunId = $parserParts[0]
            if ($parserParts.Count -ge 2) { $parserLifecycle = $parserParts[1] }
            if ($parserParts.Count -eq 3) { $parserWarnings = $parserParts[2] }
        }
        $memory = Get-Memory
        Write-RunEvent 'job_terminal' @{ sequence = $index; job_id = $jobId; state = $parts[0]; error_code = $parts[3]; error_meta = $parts[6]; attempt = $parts[4]; fence = $parts[5]; parser_run_id = $parserRunId; parser_source = $parserSource; parser_lifecycle = $parserLifecycle; parser_warnings = $parserWarnings; api_gib = $memory.api_gib; host_free_gib = $memory.host_free_gib }
        if ($parserWarnings -ne '[]') { Write-RunEvent 'parser_warning' @{ job_id = $jobId; parser_warnings = $parserWarnings } }
        Assert-SuryaReady
        if ($memory.api_gib -ge 3.6 -or $memory.host_free_gib -lt $MinHostFreeGiB) {
            Write-RunEvent 'memory_guard_after_job' @{ api_gib = $memory.api_gib; host_free_gib = $memory.host_free_gib; min_host_free_gib = $MinHostFreeGiB }
            if (-not $AutoRecoverMemory) { break }
        }
    }
    Write-RunEvent 'run_complete' @{ api_restarts = $apiRestarts }
} catch {
    Write-RunEvent 'run_stopped' @{ reason = $_.Exception.Message }
    throw
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
