param(
    [string]$LinkId = "access1--agg1",
    [string]$Output = "evidence/simulated-link-failure.json"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$intentPath = Join-Path $projectRoot "lab/intent.yml"
$outputPath = Join-Path $projectRoot $Output

python -m telconet_sentinel.demo `
    --intent $intentPath `
    --link $LinkId `
    --output $outputPath

