import type { AuthClient, AuthSession, AuthUser } from "../../types/auth";

const STORAGE_KEY = "videoscope.local-device-account.v1";
const VERSION = 1;
const ITERATIONS = 310_000;
const ADDITIONAL_DATA = new TextEncoder().encode(
  "VideoScope local device account v1",
);

type LocalAccountEnvelope = {
  version: 1;
  iterations: number;
  salt: string;
  iv: string;
  ciphertext: string;
};

type LocalAccountProfile = {
  id: string;
  displayName: string;
  createdAt: string;
};

export class LocalAccountError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LocalAccountError";
  }
}

function bytesToBase64(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function base64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const binary = atob(value);
  const result = new Uint8Array(new ArrayBuffer(binary.length));
  for (let index = 0; index < binary.length; index += 1) {
    result[index] = binary.charCodeAt(index);
  }
  return result;
}

function parseEnvelope(raw: string): LocalAccountEnvelope {
  try {
    const parsed = JSON.parse(raw) as Partial<LocalAccountEnvelope>;
    if (
      parsed.version !== VERSION ||
      parsed.iterations !== ITERATIONS ||
      typeof parsed.salt !== "string" ||
      typeof parsed.iv !== "string" ||
      typeof parsed.ciphertext !== "string"
    ) {
      throw new Error("invalid envelope");
    }
    base64ToBytes(parsed.salt);
    base64ToBytes(parsed.iv);
    base64ToBytes(parsed.ciphertext);
    return parsed as LocalAccountEnvelope;
  } catch {
    throw new LocalAccountError("The encrypted local account backup is invalid.");
  }
}

function userFromProfile(profile: LocalAccountProfile): AuthUser {
  return { id: profile.id, displayName: profile.displayName };
}

export class LocalDeviceAuthClient implements AuthClient {
  readonly mode = "local_device" as const;
  readonly #crypto: Crypto;
  readonly #storage: Storage;
  readonly #listeners = new Set<(session: AuthSession | null) => void>();
  #session: AuthSession | null = null;

  constructor(
    storage: Storage = localStorage,
    cryptoProvider: Crypto = globalThis.crypto,
  ) {
    this.#storage = storage;
    this.#crypto = cryptoProvider;
  }

  async getSession(): Promise<AuthSession | null> {
    return this.#session;
  }

  onSessionChange(callback: (session: AuthSession | null) => void) {
    this.#listeners.add(callback);
    return () => this.#listeners.delete(callback);
  }

  hasAccount(): boolean {
    return this.#storage.getItem(STORAGE_KEY) !== null;
  }

  async register(displayName: string, passphrase: string): Promise<void> {
    const normalizedName = displayName.trim();
    if (normalizedName.length < 2 || normalizedName.length > 80) {
      throw new LocalAccountError("Display name must contain 2 to 80 characters.");
    }
    if (passphrase.length < 10) {
      throw new LocalAccountError("Passphrase must contain at least 10 characters.");
    }
    const profile: LocalAccountProfile = {
      id: `local_${bytesToBase64(this.#randomBytes(18)).replaceAll(/[^a-zA-Z0-9]/g, "")}`,
      displayName: normalizedName,
      createdAt: new Date().toISOString(),
    };
    const envelope = await this.#encrypt(profile, passphrase);
    this.#storage.setItem(STORAGE_KEY, JSON.stringify(envelope));
    this.#setSession({ user: userFromProfile(profile) });
  }

  async signIn(passphrase: string): Promise<void> {
    const raw = this.#storage.getItem(STORAGE_KEY);
    if (!raw) throw new LocalAccountError("No local account exists on this device.");
    const envelope = parseEnvelope(raw);
    try {
      const key = await this.#deriveKey(
        passphrase,
        base64ToBytes(envelope.salt),
        ["decrypt"],
      );
      const plaintext = await this.#crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: base64ToBytes(envelope.iv),
          additionalData: ADDITIONAL_DATA,
        },
        key,
        base64ToBytes(envelope.ciphertext),
      );
      const profile = JSON.parse(
        new TextDecoder().decode(plaintext),
      ) as Partial<LocalAccountProfile>;
      if (
        typeof profile.id !== "string" ||
        typeof profile.displayName !== "string" ||
        typeof profile.createdAt !== "string"
      ) {
        throw new Error("invalid profile");
      }
      this.#setSession({ user: userFromProfile(profile as LocalAccountProfile) });
    } catch {
      throw new LocalAccountError("The passphrase is incorrect or the backup is damaged.");
    }
  }

  exportEncryptedBackup(): string {
    const raw = this.#storage.getItem(STORAGE_KEY);
    if (!raw) throw new LocalAccountError("No local account exists on this device.");
    parseEnvelope(raw);
    return raw;
  }

  importEncryptedBackup(raw: string): void {
    const envelope = parseEnvelope(raw);
    this.#storage.setItem(STORAGE_KEY, JSON.stringify(envelope));
    this.#setSession(null);
  }

  deleteAccount(): void {
    this.#storage.removeItem(STORAGE_KEY);
    this.#setSession(null);
  }

  async signOut(): Promise<void> {
    this.#setSession(null);
  }

  async signInWithMagicLink(): Promise<void> {
    throw new LocalAccountError("Cloud sign-in is not configured for this deployment.");
  }

  async signInWithGitHub(): Promise<void> {
    throw new LocalAccountError("Cloud sign-in is not configured for this deployment.");
  }

  async completeCallback(): Promise<void> {
    throw new LocalAccountError("Cloud sign-in is not configured for this deployment.");
  }

  async #encrypt(
    profile: LocalAccountProfile,
    passphrase: string,
  ): Promise<LocalAccountEnvelope> {
    const salt = this.#randomBytes(16);
    const iv = this.#randomBytes(12);
    const key = await this.#deriveKey(passphrase, salt, ["encrypt"]);
    const ciphertext = await this.#crypto.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: ADDITIONAL_DATA },
      key,
      new TextEncoder().encode(JSON.stringify(profile)),
    );
    return {
      version: VERSION,
      iterations: ITERATIONS,
      salt: bytesToBase64(salt),
      iv: bytesToBase64(iv),
      ciphertext: bytesToBase64(new Uint8Array(ciphertext)),
    };
  }

  async #deriveKey(
    passphrase: string,
    salt: Uint8Array<ArrayBuffer>,
    usages: KeyUsage[],
  ): Promise<CryptoKey> {
    const material = await this.#crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(passphrase),
      "PBKDF2",
      false,
      ["deriveKey"],
    );
    return this.#crypto.subtle.deriveKey(
      { name: "PBKDF2", salt, iterations: ITERATIONS, hash: "SHA-256" },
      material,
      { name: "AES-GCM", length: 256 },
      false,
      usages,
    );
  }

  #randomBytes(length: number): Uint8Array<ArrayBuffer> {
    return this.#crypto.getRandomValues(new Uint8Array(new ArrayBuffer(length)));
  }

  #setSession(session: AuthSession | null): void {
    this.#session = session;
    for (const listener of this.#listeners) listener(session);
  }
}

export function isLocalDeviceAuthClient(
  client: AuthClient,
): client is LocalDeviceAuthClient {
  return client instanceof LocalDeviceAuthClient;
}
