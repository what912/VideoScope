import { describe, expect, it } from "vitest";

import {
  OFFICIAL_V082_INSTALLER_URL,
  OFFICIAL_V082_INSTALLER_SHA256,
  RELEASES_URL,
  resolveWindowsInstaller,
} from "./connector-install";

const OTHER_SHA256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

describe("resolveWindowsInstaller", () => {
  it("enables the direct installer only for the exact published URL and SHA-256", () => {
    expect(
      resolveWindowsInstaller(
        OFFICIAL_V082_INSTALLER_URL,
        OFFICIAL_V082_INSTALLER_SHA256,
      ),
    ).toEqual({
      hasDirectWindowsInstaller: true,
      windowsInstallerSha256: OFFICIAL_V082_INSTALLER_SHA256,
      windowsInstallerUrl: OFFICIAL_V082_INSTALLER_URL,
    });
  });

  it.each([
    [undefined, OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL, undefined],
    [OFFICIAL_V082_INSTALLER_URL, OFFICIAL_V082_INSTALLER_SHA256.toUpperCase()],
    [OFFICIAL_V082_INSTALLER_URL, OTHER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL, "a".repeat(63)],
    [OFFICIAL_V082_INSTALLER_URL, `${"a".repeat(63)}g`],
    [OFFICIAL_V082_INSTALLER_URL.replace("https:", "http:"), OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL.replace("github.com", "evil.example"), OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL.replace("what912", "WHAT912"), OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL.replace("VideoScope", "videoscope"), OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL.replace("v0.8.2", "v0.8.1"), OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL.replace("VideoScope-Setup-x64.exe", "setup.exe"), OFFICIAL_V082_INSTALLER_SHA256],
    [`${OFFICIAL_V082_INSTALLER_URL}?download=1`, OFFICIAL_V082_INSTALLER_SHA256],
    [`${OFFICIAL_V082_INSTALLER_URL}#sha256`, OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL.replace("github.com", "user@github.com"), OFFICIAL_V082_INSTALLER_SHA256],
    [OFFICIAL_V082_INSTALLER_URL.replace("github.com", "github.com:444"), OFFICIAL_V082_INSTALLER_SHA256],
    [` ${OFFICIAL_V082_INSTALLER_URL}`, OFFICIAL_V082_INSTALLER_SHA256],
  ])("falls back for an unsafe or incomplete pair: %s", (url, sha256) => {
    expect(resolveWindowsInstaller(url, sha256)).toEqual({
      hasDirectWindowsInstaller: false,
      windowsInstallerSha256: undefined,
      windowsInstallerUrl: RELEASES_URL,
    });
  });
});
