export const RELEASES_URL = "https://github.com/what912/VideoScope/releases";
export const OFFICIAL_V081_INSTALLER_URL =
  "https://github.com/what912/VideoScope/releases/download/v0.8.1/VideoScope-Setup-x64.exe";
const SHA256 = /^[0-9a-f]{64}$/u;

export interface WindowsInstallerResolution {
  hasDirectWindowsInstaller: boolean;
  windowsInstallerSha256: string | undefined;
  windowsInstallerUrl: string;
}

export function resolveWindowsInstaller(
  configuredUrl: string | undefined,
  configuredSha256: string | undefined,
): WindowsInstallerResolution {
  if (
    configuredUrl === OFFICIAL_V081_INSTALLER_URL
    && configuredSha256 !== undefined
    && SHA256.test(configuredSha256)
  ) {
    return {
      hasDirectWindowsInstaller: true,
      windowsInstallerSha256: configuredSha256,
      windowsInstallerUrl: configuredUrl,
    };
  }
  return {
    hasDirectWindowsInstaller: false,
    windowsInstallerSha256: undefined,
    windowsInstallerUrl: RELEASES_URL,
  };
}

const installer = resolveWindowsInstaller(
  import.meta.env.VITE_WINDOWS_INSTALLER_URL,
  import.meta.env.VITE_WINDOWS_INSTALLER_SHA256,
);

export const connectorInstall = {
  ...installer,
  releasesUrl: RELEASES_URL,
  startProtocolUrl: "videoscope://start",
  legacyStartCommand: "videoscope serve --port 8765",
  legacyInstallCommands: [
    '$videoScopeRoot = Join-Path $env:LOCALAPPDATA "VideoScope"',
    'py -3.12 -m venv "$videoScopeRoot\\.venv"',
    '& "$videoScopeRoot\\.venv\\Scripts\\python.exe" -m pip install --upgrade pip',
    '& "$videoScopeRoot\\.venv\\Scripts\\python.exe" -m pip install "genvideoscope[web] @ https://github.com/what912/VideoScope/releases/download/v0.8.0/genvideoscope-0.8.0-py3-none-any.whl"',
    '& "$videoScopeRoot\\.venv\\Scripts\\python.exe" -m videoscope serve --port 8765',
  ].join("\n"),
} as const;

export type ConnectorInstall = typeof connectorInstall;
