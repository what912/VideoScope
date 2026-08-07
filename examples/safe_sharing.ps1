param(
    [Parameter(Mandatory = $true)]
    [string]$InputVideo,

    [string]$OutputDirectory = "runs/safe-sharing",

    [string]$ReviewFile = "",

    [string]$ConfirmDigest = ""
)

$ErrorActionPreference = "Stop"

if ($ConfirmDigest) {
    videoscope privacy $InputVideo `
        --output $OutputDirectory `
        --confirm-digest $ConfirmDigest
    exit $LASTEXITCODE
}

if ($ReviewFile) {
    if (-not (Test-Path -LiteralPath $ReviewFile)) {
        throw "Review file not found: $ReviewFile"
    }
    videoscope privacy $InputVideo `
        --output $OutputDirectory `
        --review-file $ReviewFile `
        --preview-only
    exit $LASTEXITCODE
}

videoscope privacy $InputVideo `
    --output $OutputDirectory `
    --audience public `
    --scan-only
exit $LASTEXITCODE
