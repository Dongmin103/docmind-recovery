[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExecutablePath,

    [Parameter(Mandatory = $true)]
    [string]$EncryptedInputPath,

    [string]$OutputFileName = 'validated-output.bin',
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$ExpectedPlaintextSha256,
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 120,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.local/uEncryptor2'))
$sampleRoot = [IO.Path]::GetFullPath((Join-Path $localRoot 'samples'))
$jobRoot = [IO.Path]::GetFullPath((Join-Path $localRoot 'jobs'))
$reportRoot = [IO.Path]::GetFullPath((Join-Path $localRoot 'reports'))
$executable = [IO.Path]::GetFullPath($ExecutablePath)
$encryptedInput = [IO.Path]::GetFullPath($EncryptedInputPath)

function Assert-ChildPath([string]$Path, [string]$Parent, [string]$Label) {
    $prefix = $Parent.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay below $Parent"
    }
}

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "uEncryptor2 executable not found: $executable"
}
if ([IO.Path]::GetExtension($executable) -ne '.exe') {
    throw 'ExecutablePath must point to a Windows .exe file.'
}
if (-not (Test-Path -LiteralPath $encryptedInput -PathType Leaf)) {
    throw "Non-sensitive encrypted sample not found: $encryptedInput"
}
Assert-ChildPath $encryptedInput $sampleRoot 'EncryptedInputPath'
if ([IO.Path]::GetFileName($OutputFileName) -ne $OutputFileName -or $OutputFileName.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
    throw 'OutputFileName must be a plain file name without a directory component.'
}
if ($executable.Contains('"') -or $encryptedInput.Contains('"')) {
    throw 'Paths containing quotation marks are not supported.'
}

$executableHash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
$inputHash = (Get-FileHash -LiteralPath $encryptedInput -Algorithm SHA256).Hash.ToLowerInvariant()
$version = [Diagnostics.FileVersionInfo]::GetVersionInfo($executable).FileVersion

if (-not $Execute) {
    [pscustomobject]@{
        mode = 'dry-run'
        executable_sha256 = $executableHash
        executable_version = $version
        sample_sha256 = $inputHash
        sample_root_validated = $true
        command_shape = 'uEncryptor2.exe <sample> <new-job-output> /dec'
        executed = $false
    } | ConvertTo-Json
    return
}
if (-not $ExpectedPlaintextSha256) {
    throw 'ExpectedPlaintextSha256 is required with -Execute; output existence alone is not a success criterion.'
}

$jobId = [Guid]::NewGuid().ToString('n')
$jobDirectory = [IO.Path]::GetFullPath((Join-Path $jobRoot $jobId))
Assert-ChildPath $jobDirectory $jobRoot 'Job directory'
$outputPath = [IO.Path]::GetFullPath((Join-Path $jobDirectory $OutputFileName))
Assert-ChildPath $outputPath $jobDirectory 'Output path'
$reportPath = [IO.Path]::GetFullPath((Join-Path $reportRoot "$jobId.json"))
$startedAt = [DateTimeOffset]::UtcNow
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
$exitCode = $null
$timedOut = $false
$outputHash = $null
$outputBytes = 0
$stdoutHash = $null
$stderrHash = $null
$cleanupComplete = $false
$mutex = $null
$mutexAcquired = $false
$process = $null
$errorCode = $null

try {
    [IO.Directory]::CreateDirectory($jobDirectory) | Out-Null
    if (Test-Path -LiteralPath $outputPath) {
        throw 'The job output path must not already exist.'
    }

    $createdNew = $false
    $mutex = [Threading.Mutex]::new($false, 'Global\DocMind-uEncryptor2-SingleRun', [ref]$createdNew)
    $mutexAcquired = $mutex.WaitOne([TimeSpan]::FromSeconds(5))
    if (-not $mutexAcquired) {
        throw 'Another uEncryptor2 validation job owns the host-wide single-run mutex.'
    }

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $executable
    $startInfo.Arguments = ('"{0}" "{1}" /dec' -f $encryptedInput, $outputPath)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw 'uEncryptor2 process did not start.'
    }
    $process.StandardInput.Close()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $timedOut = $true
        $errorCode = 'TIMEOUT_OR_INTERACTIVE_PROMPT'
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
        $process.WaitForExit()
    }
    $exitCode = $process.ExitCode
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $stdoutHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($stdout))) -replace '-', '').ToLowerInvariant()
        $stderrHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($stderr))) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }

    if (-not $timedOut) {
        if ($exitCode -ne 0) {
            $errorCode = 'NONZERO_EXIT'
        } elseif (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
            $errorCode = 'OUTPUT_MISSING'
        } else {
            $outputFile = Get-Item -LiteralPath $outputPath
            $outputBytes = $outputFile.Length
            $outputHash = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($outputBytes -le 0) {
                $errorCode = 'OUTPUT_EMPTY'
            } elseif ($outputHash -ne $ExpectedPlaintextSha256.ToLowerInvariant()) {
                $errorCode = 'PLAINTEXT_HASH_MISMATCH'
            }
        }
    }
} catch {
    if (-not $errorCode) {
        $errorCode = 'PROBE_ERROR'
    }
    throw
} finally {
    $stopwatch.Stop()
    if ($process) {
        $process.Dispose()
    }
    if ($mutexAcquired) {
        $mutex.ReleaseMutex()
    }
    if ($mutex) {
        $mutex.Dispose()
    }
    if (Test-Path -LiteralPath $jobDirectory) {
        Assert-ChildPath $jobDirectory $jobRoot 'Cleanup target'
        Remove-Item -LiteralPath $jobDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    $cleanupComplete = -not (Test-Path -LiteralPath $jobDirectory)
    [IO.Directory]::CreateDirectory($reportRoot) | Out-Null
    $report = [ordered]@{
        schema_version = 1
        job_id = $jobId
        started_at_utc = $startedAt.ToString('o')
        duration_ms = $stopwatch.ElapsedMilliseconds
        executable_sha256 = $executableHash
        executable_version = $version
        encrypted_sample_sha256 = $inputHash
        exit_code = $exitCode
        timed_out = $timedOut
        output_bytes = $outputBytes
        output_sha256 = $outputHash
        expected_plaintext_sha256 = $ExpectedPlaintextSha256.ToLowerInvariant()
        stdout_sha256 = $stdoutHash
        stderr_sha256 = $stderrHash
        output_content_logged = $false
        cleanup_complete = $cleanupComplete
        probe_passed = (-not $errorCode -and $cleanupComplete)
        error_code = $errorCode
        automation_decision = 'not-determined-by-single-run'
    }
    [IO.File]::WriteAllText($reportPath, ($report | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Write-Output "Validation report: $reportPath"
}

if ($errorCode) {
    throw "uEncryptor2 validation failed: $errorCode"
}
Write-Output 'Single non-sensitive sample run validated; login, reboot, repetition, and vendor-support conditions remain to be tested.'
