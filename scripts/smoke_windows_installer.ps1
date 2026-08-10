[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$temporaryRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $systemTemp ("videoscope-installer-smoke-" + [guid]::NewGuid().ToString("N")))
)
if (-not $temporaryRoot.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Smoke-test directory escaped the system temporary directory."
}
$installRoot = Join-Path $temporaryRoot "VideoScope"
New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
$connectorProcess = $null

try {
    $setup = Start-Process -FilePath $installer -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=$installRoot"
    ) -Wait -PassThru -WindowStyle Hidden
    if ($setup.ExitCode -ne 0) { throw "Silent installer returned $($setup.ExitCode)." }

    $connector = Join-Path $installRoot "VideoScopeConnector.exe"
    if (-not (Test-Path -LiteralPath $connector)) { throw "Installed connector is missing." }
    if (Test-Path -LiteralPath (Join-Path $installRoot "ffmpeg.exe")) { throw "Installer bundled ffmpeg.exe." }
    if (Test-Path -LiteralPath (Join-Path $installRoot "ffprobe.exe")) { throw "Installer bundled ffprobe.exe." }

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()

    $connectorProcess = Start-Process -FilePath $connector -ArgumentList @(
        "--headless",
        "--port",
        $port
    ) -PassThru -WindowStyle Hidden

    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    $health = $null
    while ([DateTime]::UtcNow -lt $deadline -and -not $connectorProcess.HasExited) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
            break
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if ($null -eq $health -or $health.local_only_default -ne $true) {
        $connectorState = if ($connectorProcess.HasExited) {
            "process exited with code $($connectorProcess.ExitCode)"
        } else {
            "process remained active without a healthy loopback response"
        }
        throw "Installed connector health check failed: $connectorState."
    }

    $shutdown = Start-Process -FilePath $connector -ArgumentList "--shutdown" -Wait -PassThru -WindowStyle Hidden
    if ($shutdown.ExitCode -ne 0) { throw "Controlled shutdown returned $($shutdown.ExitCode)." }
    if (-not $connectorProcess.WaitForExit(15000)) { throw "Connector did not stop cleanly." }

    $uninstaller = Join-Path $installRoot "unins000.exe"
    $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART"
    ) -Wait -PassThru -WindowStyle Hidden
    if ($uninstall.ExitCode -ne 0) { throw "Silent uninstall returned $($uninstall.ExitCode)." }
    if (Test-Path -LiteralPath $connector) { throw "Uninstall left the connector executable behind." }
    Write-Host "PASS Windows installer, loopback health, controlled shutdown, and uninstall"
} finally {
    if ($null -ne $connectorProcess -and -not $connectorProcess.HasExited) {
        Stop-Process -Id $connectorProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $temporaryRoot) {
        $resolvedTemporaryRoot = (Resolve-Path -LiteralPath $temporaryRoot).Path
        if (-not $resolvedTemporaryRoot.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a smoke-test path outside the system temporary directory."
        }
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
