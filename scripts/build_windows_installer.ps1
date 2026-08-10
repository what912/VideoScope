[CmdletBinding()]
param(
    [string]$PythonCommand = "python",
    [string]$IsccPath = "",
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$packagingRoot = Join-Path $repositoryRoot "packaging\windows"
$buildRoot = Join-Path $repositoryRoot "build\windows"
$distRoot = Join-Path $buildRoot "dist"
$workRoot = Join-Path $buildRoot "work"
$installerRoot = Join-Path $buildRoot "installer"
$bundleRoot = Join-Path $distRoot "VideoScopeConnector"

New-Item -ItemType Directory -Force -Path $buildRoot, $distRoot, $workRoot, $installerRoot | Out-Null

if (-not $SkipDependencyInstall) {
    & $PythonCommand -m pip install --disable-pip-version-check --requirement (Join-Path $packagingRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Windows build dependency installation failed." }
    & $PythonCommand -m pip install --disable-pip-version-check -e "${repositoryRoot}[web]"
    if ($LASTEXITCODE -ne 0) { throw "VideoScope Web runtime installation failed." }
}

$version = (& $PythonCommand -c "from videoscope import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
    throw "Unable to read the VideoScope version."
}
$numericParts = [regex]::Matches($version, "\d+") | ForEach-Object { $_.Value }
while ($numericParts.Count -lt 4) { $numericParts += "0" }
$versionInfoVersion = ($numericParts | Select-Object -First 4) -join "."

& $PythonCommand -m PyInstaller `
    --noconfirm `
    --clean `
    --log-level WARN `
    --distpath $distRoot `
    --workpath $workRoot `
    (Join-Path $packagingRoot "VideoScopeConnector.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

& $PythonCommand (Join-Path $repositoryRoot "scripts\audit_windows_bundle.py") $bundleRoot
if ($LASTEXITCODE -ne 0) { throw "Frozen bundle audit failed." }

if ([string]::IsNullOrWhiteSpace($IsccPath)) {
    $isccCommand = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if ($null -ne $isccCommand) {
        $IsccPath = $isccCommand.Source
    } else {
        $innoCandidates = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
        )
        foreach ($candidate in $innoCandidates) {
            if (Test-Path -LiteralPath $candidate) {
                $IsccPath = $candidate
                break
            }
        }
    }
}
if ([string]::IsNullOrWhiteSpace($IsccPath) -or -not (Test-Path -LiteralPath $IsccPath)) {
    throw "Inno Setup 6 compiler was not found. Pass -IsccPath explicitly."
}

& $IsccPath `
    "/Qp" `
    "/DMyAppVersion=$version" `
    "/DMyVersionInfoVersion=$versionInfoVersion" `
    "/DMyBundleDir=$bundleRoot" `
    "/DMyOutputDir=$installerRoot" `
    (Join-Path $packagingRoot "VideoScope.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }

$installer = Join-Path $installerRoot "VideoScope-Setup-x64.exe"
if (-not (Test-Path -LiteralPath $installer)) { throw "Installer output is missing." }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$installer.sha256" -Encoding ascii -Value "$hash  VideoScope-Setup-x64.exe"
Write-Host "Built $installer"
