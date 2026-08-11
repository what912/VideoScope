import { webcrypto } from "node:crypto";
import { beforeEach, describe, expect, it } from "vitest";

import {
  LocalAccountError,
  LocalDeviceAuthClient,
} from "./local-device-auth-client";

describe("encrypted local device account", () => {
  beforeEach(() => localStorage.clear());

  it("encrypts the profile, unlocks it, and never stores the passphrase", async () => {
    const client = new LocalDeviceAuthClient(
      localStorage,
      webcrypto as unknown as Crypto,
    );
    await client.register("Video Maker", "a-long-local-passphrase");
    const stored = localStorage.getItem("videoscope.local-device-account.v1");
    expect(stored).not.toContain("Video Maker");
    expect(stored).not.toContain("a-long-local-passphrase");
    await client.signOut();
    await expect(client.getSession()).resolves.toBeNull();
    await client.signIn("a-long-local-passphrase");
    expect((await client.getSession())?.user.displayName).toBe("Video Maker");
  });

  it("rejects a wrong passphrase and supports encrypted migration", async () => {
    const cryptoProvider = webcrypto as unknown as Crypto;
    const first = new LocalDeviceAuthClient(localStorage, cryptoProvider);
    await first.register("Research User", "correct-passphrase-123");
    const backup = first.exportEncryptedBackup();
    await first.signOut();
    await expect(first.signIn("incorrect-passphrase")).rejects.toBeInstanceOf(
      LocalAccountError,
    );

    localStorage.clear();
    const second = new LocalDeviceAuthClient(localStorage, cryptoProvider);
    second.importEncryptedBackup(backup);
    await second.signIn("correct-passphrase-123");
    expect((await second.getSession())?.user.displayName).toBe("Research User");
    second.deleteAccount();
    expect(second.hasAccount()).toBe(false);
  });

  it("rejects malformed backups and short secrets", async () => {
    const client = new LocalDeviceAuthClient(
      localStorage,
      webcrypto as unknown as Crypto,
    );
    await expect(client.register("A", "short")).rejects.toBeInstanceOf(
      LocalAccountError,
    );
    expect(() => client.importEncryptedBackup('{"api_key":"secret"}')).toThrow(
      LocalAccountError,
    );
    expect(() => client.importEncryptedBackup("x".repeat(16 * 1024 + 1))).toThrow(
      LocalAccountError,
    );
    await expect(
      client.register("Valid name", "x".repeat(257)),
    ).rejects.toBeInstanceOf(LocalAccountError);
    expect(localStorage.getItem("videoscope.local-device-account.v1")).toBeNull();
  });
});
