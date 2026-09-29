[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$scriptPath = Join-Path $PSScriptRoot 'Get-DocMindSourceMetadataManifest.ps1'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$testRoot = Join-Path $repoRoot ('.local/metadata-manifest-test-' + [Guid]::NewGuid().ToString('n'))
$sourceRoot = Join-Path $testRoot 'source'
$outputRoot = Join-Path $testRoot 'output'
[void][IO.Directory]::CreateDirectory($sourceRoot)
[void][IO.Directory]::CreateDirectory($outputRoot)
[void][IO.Directory]::CreateDirectory((Join-Path $sourceRoot 'chosen'))
[IO.File]::WriteAllText((Join-Path $sourceRoot 'chosen/private.doc'), 'sensitive fixture')
[IO.File]::WriteAllText((Join-Path $sourceRoot 'chosen/skip.txt'), 'exclude')
[IO.File]::WriteAllText((Join-Path $sourceRoot 'outside.pdf'), 'not selected')

function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Assert-Throws([scriptblock]$Action, [string]$Message) {
    try { & $Action | Out-Null; throw $Message } catch { if ($_.Exception.Message -eq $Message) { throw } }
}

$firstPath = Join-Path $outputRoot 'first.json'
$secondPath = Join-Path $outputRoot 'second.json'
& $scriptPath -Root $sourceRoot -RelativePaths @('chosen') -OutputPath $firstPath -MaxBytes 1 | Out-Null
$first = Get-Content -LiteralPath $firstPath -Raw | ConvertFrom-Json
Assert-True ($first.entries.Count -eq 2) 'Selected folder did not produce exactly two files.'
Assert-True ((@($first.entries | Where-Object { $_.extension -eq '.doc' -and $_.status -eq 'oversize' })).Count -eq 1) 'Oversize file was not classified.'
Assert-True ((@($first.entries | Where-Object { $_.extension -eq '.txt' -and $_.status -eq 'unsupported' })).Count -eq 1) 'Unsupported file was not classified.'
Assert-True (-not ((Get-Content -LiteralPath $firstPath -Raw) -match 'private|sensitive|outside|chosen')) 'Manifest leaked source names or content.'
Assert-True ((Test-Path -LiteralPath (Join-Path $outputRoot '.manifest-id-key') -PathType Leaf)) 'Private ID key was not saved.'

& $scriptPath -Root $sourceRoot -RelativePaths @('chosen') -OutputPath $secondPath -PreviousManifestPath $firstPath -MaxBytes 64MB | Out-Null
$second = Get-Content -LiteralPath $secondPath -Raw | ConvertFrom-Json
$doc = @($second.entries | Where-Object { $_.extension -eq '.doc' })[0]
Assert-True ($doc.status -eq 'eligible_metadata' -and $doc.stability -eq 'stable') 'Stable metadata was not recognized.'
Assert-True ($doc.id -eq @($first.entries | Where-Object { $_.extension -eq '.doc' })[0].id) 'Opaque ID changed between snapshots.'

[IO.File]::AppendAllText((Join-Path $sourceRoot 'chosen/private.doc'), ' changed')
$thirdPath = Join-Path $outputRoot 'third.json'
& $scriptPath -Root $sourceRoot -RelativePaths @('chosen/private.doc', 'chosen') -OutputPath $thirdPath -PreviousManifestPath $secondPath | Out-Null
$third = Get-Content -LiteralPath $thirdPath -Raw | ConvertFrom-Json
Assert-True ($third.entries.Count -eq 2) 'Overlapping selection created duplicate entries.'
Assert-True ((@($third.entries | Where-Object { $_.extension -eq '.doc' })[0].stability) -eq 'changed') 'Changed metadata was not detected.'

Assert-Throws { & $scriptPath -Root $sourceRoot -RelativePaths @('../outside.pdf') -OutputPath (Join-Path $outputRoot 'escape.json') } 'Traversal was accepted.'
Assert-Throws { & $scriptPath -Root $sourceRoot -RelativePaths @('chosen') -OutputPath (Join-Path $sourceRoot 'manifest.json') } 'Output inside source was accepted.'
Write-Output 'Metadata manifest synthetic checks passed.'
