import { describe, expect, it } from "vitest";

import {
  OFFICIAL_V081_INSTALLER_URL,
  RELEASES_URL,
  resolveWindowsInstaller,
} from "./connector-install";

const SHA256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

describe("resolveWindowsInstaller", () => {
  it("enables the direct installer only for the exact official URL and lowercase SHA-256", () => {
    expect(resolveWindowsInstaller(OFFICIAL_V081_INSTALLER_URL, SHA256)).toEqual({
      hasDirectWindowsInstaller: true,
      windowsInstallerSha256: SHA256,
      windowsInstallerUrl: OFFICIAL_V081_INSTALLER_URL,
    });
  });

  it.each([
    [undefined, SHA256],
    [OFFICIAL_V081_INSTALLER_URL, undefined],
    [OFFICIAL_V081_INSTALLER_URL, SHA256.toUpperCase()],
    [OFFICIAL_V081_INSTALLER_URL, "a".repeat(63)],
    [OFFICIAL_V081_INSTALLER_URL, `${"a".repeat(63)}g`],
    [OFFICIAL_V081_INSTALLER_URL.replace("https:", "http:"), SHA256],
    [OFFICIAL_V081_INSTALLER_URL.replace("github.com", "evil.example"), SHA256],
    [OFFICIAL_V081_INSTALLER_URL.replace("what912", "WHAT912"), SHA256],
    [OFFICIAL_V081_INSTALLER_URL.replace("VideoScope", "videoscope"), SHA256],
    [OFFICIAL_V081_INSTALLER_URL.replace("v0.8.1", "v0.8.0"), SHA256],
    [OFFICIAL_V081_INSTALLER_URL.replace("VideoScope-Setup-x64.exe", "setup.exe"), SHA256],
    [`${OFFICIAL_V081_INSTALLER_URL}?download=1`, SHA256],
    [`${OFFICIAL_V081_INSTALLER_URL}#sha256`, SHA256],
    [OFFICIAL_V081_INSTALLER_URL.replace("github.com", "user@github.com"), SHA256],
    [OFFICIAL_V081_INSTALLER_URL.replace("github.com", "github.com:444"), SHA256],
    [` ${OFFICIAL_V081_INSTALLER_URL}`, SHA256],
  ])("falls back for an unsafe or incomplete pair: %s", (url, sha256) => {
    expect(resolveWindowsInstaller(url, sha256)).toEqual({
      hasDirectWindowsInstaller: false,
      windowsInstallerSha256: undefined,
      windowsInstallerUrl: RELEASES_URL,
    });
  });
});
