import type {
  CreateShareResult,
  ShareClient,
} from "./contracts";

export class ShareUnavailableError extends Error {
  constructor() {
    super("Optional report sharing is not configured.");
    this.name = "ShareUnavailableError";
  }
}

export class UnavailableShareClient implements ShareClient {
  readonly availability = "unavailable" as const;

  async createShare(): Promise<CreateShareResult> {
    throw new ShareUnavailableError();
  }

  async getSharedReport(): Promise<null> {
    throw new ShareUnavailableError();
  }

  async revokeShare(): Promise<void> {
    throw new ShareUnavailableError();
  }
}

export function isUnavailableShareClient(
  client: ShareClient,
): client is UnavailableShareClient {
  return client.availability === "unavailable";
}
