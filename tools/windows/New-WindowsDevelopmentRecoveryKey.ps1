[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'WindowsDevelopmentRecovery.Common.ps1')

$resolvedOutput = Assert-RecoveryPath -Path $OutputPath -Purpose 'Recovery key output'
if (Test-Path -LiteralPath $resolvedOutput) {
    throw "Refusing to overwrite recovery key: $resolvedOutput"
}
[IO.Directory]::CreateDirectory((Split-Path -Parent $resolvedOutput)) | Out-Null
$key = [byte[]]::new(64)
$generator = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $generator.GetBytes($key)
} finally {
    $generator.Dispose()
}
$document = [ordered]@{
    format = 'docmind-recovery-key/v1'
    createdUtc = [DateTime]::UtcNow.ToString('o')
    key = [Convert]::ToBase64String($key)
}
[IO.File]::WriteAllText(
    $resolvedOutput,
    ($document | ConvertTo-Json -Compress),
    [Text.UTF8Encoding]::new($false)
)

if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $resolvedOutput /inheritance:r /grant:r "*$sid`:F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $resolvedOutput -Force
        throw 'Unable to restrict the recovery key ACL to the current Windows user.'
    }
}
Write-Output "Created recovery key with overwrite protection: $resolvedOutput"
Write-Output 'Store this key separately from recovery packages. Its value was not printed.'
