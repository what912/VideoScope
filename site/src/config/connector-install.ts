const RELEASES_URL = "https://github.com/what912/VideoScope/releases";

const configuredInstallerUrl = import.meta.env.VITE_WINDOWS_INSTALLER_URL?.trim();

export const connectorInstall = {
  releasesUrl: RELEASES_URL,
  windowsInstallerUrl: configuredInstallerUrl || RELEASES_URL,
  hasDirectWindowsInstaller: Boolean(configuredInstallerUrl),
  startProtocolUrl: "videoscope://start",
  legacyStartCommand: "videoscope serve --port 8765",
  legacyInstallCommands: [
    '$videoScopeRoot = Join-Path $env:LOCALAPPDATA "VideoScope"',
    'py -3.12 -m venv "$videoScopeRoot\\.venv"',
    '& "$videoScopeRoot\\.venv\\Scripts\\python.exe" -m pip install --upgrade pip',
    '& "$videoScopeRoot\\.venv\\Scripts\\python.exe" -m pip install "genvideoscope[web] @ https://github.com/what912/VideoScope/releases/download/v0.8.0.dev0/genvideoscope-0.8.0.dev0-py3-none-any.whl"',
    '& "$videoScopeRoot\\.venv\\Scripts\\python.exe" -m videoscope serve --port 8765',
  ].join("\n"),
} as const;
