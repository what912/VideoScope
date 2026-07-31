import type {
  CreateShareRequest,
  CreateShareResult,
  ShareClient,
} from "./contracts";

export class FakeShareClient implements ShareClient {
  readonly availability = "configured" as const;
  readonly readPublicIds: string[] = [];
  readonly requests: CreateShareRequest[] = [];
  readonly revokedPublicIds: string[] = [];
  readonly #reports = new Map<string, CreateShareRequest["report"]>();

  async createShare(request: CreateShareRequest): Promise<CreateShareResult> {
    this.requests.push(structuredClone(request));
    const publicId = "00000000-0000-4000-8000-000000000001";
    this.#reports.set(publicId, structuredClone(request.report));
    return {
      createdAt: "2026-07-30T08:00:00.000Z",
      expiresAt: request.expiresAt,
      publicId,
    };
  }

  async getSharedReport(publicId: string) {
    this.readPublicIds.push(publicId);
    const report = this.#reports.get(publicId);
    return report ? structuredClone(report) : null;
  }

  async revokeShare(publicId: string) {
    this.revokedPublicIds.push(publicId);
    this.#reports.delete(publicId);
  }

  seed(publicId: string, report: CreateShareRequest["report"]) {
    this.#reports.set(publicId, structuredClone(report));
  }
}
