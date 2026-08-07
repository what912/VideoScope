import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectorProviderProfile } from "../types";
import { ProviderSettings } from "./ProviderSettings";

const SECRET = "user-owned-test-secret";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();
  get length(): number { return this.values.size; }
  clear(): void { this.values.clear(); }
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string): void { this.values.delete(key); }
  setItem(key: string, value: string): void { this.values.set(key, value); }
}

function storageText(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, index) => {
    const key = storage.key(index) ?? "";
    return `${key}:${storage.getItem(key) ?? ""}`;
  }).join("|");
}

describe("ProviderSettings", () => {
  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: new MemoryStorage(),
    });
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: new MemoryStorage(),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends a BYOK key once, clears the field, and never persists it", async () => {
    const saved: ConnectorProviderProfile[] = [];
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        const path = String(input);
        if (init?.method === "PUT") {
          const request = JSON.parse(String(init.body)) as Record<string, unknown>;
          expect(request.api_key).toBe(SECRET);
          saved.splice(0, saved.length, {
            ...(request as unknown as Omit<ConnectorProviderProfile, "credential_state">),
            credential_state: "memory_only",
          });
          return new Response(JSON.stringify(saved[0]), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        expect(path).toContain("/api/connector/providers");
        return new Response(JSON.stringify(saved), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    );
    const onProfiles = vi.fn();
    const user = userEvent.setup();
    render(<ProviderSettings locale="en" onProfiles={onProfiles} />);

    await waitFor(() => expect(fetcher).toHaveBeenCalled());
    await user.selectOptions(
      screen.getByLabelText("Compatibility preset"),
      "openai",
    );
    await user.type(screen.getByLabelText("Exact model ID"), "gpt-user-model");
    await user.type(screen.getByLabelText("API key (never stored on disk)"), SECRET);
    await user.click(
      screen.getByRole("button", { name: "Keep in memory for this session" }),
    );

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Provider is ready in connector memory.",
    );
    expect(screen.getByLabelText("API key (never stored on disk)")).toHaveValue("");
    expect(storageText(localStorage)).not.toContain(SECRET);
    expect(storageText(sessionStorage)).not.toContain(SECRET);
    expect(onProfiles).toHaveBeenLastCalledWith([
      expect.objectContaining({ model_id: "gpt-user-model" }),
    ]);
  });
});
