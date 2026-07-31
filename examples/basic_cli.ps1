param(
    [Parameter(Mandatory = $true)]
    [string]$InputVideo,

    [string]$OutputDirectory = "runs/basic-cli"
)

$ErrorActionPreference = "Stop"

videoscope --version
videoscope doctor
videoscope analyze $InputVideo --output $OutputDirectory

Write-Host "JSON report: $OutputDirectory/report.json"
Write-Host "HTML report: $OutputDirectory/report.html"
