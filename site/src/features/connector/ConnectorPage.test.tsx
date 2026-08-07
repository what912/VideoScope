import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../../i18n/I18nProvider";
import { connectorClient } from "../../services/connector/connector-client";
import { ConnectorPage } from "./ConnectorPage";

describe("ConnectorPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("exposes every full local workflow only after a paired connector responds", async () => {
    vi.spyOn(connectorClient, "status").mockResolvedValue({
      status: "ready",
      service: "VideoScope Local Connector",
      version: "0.8.0",
      pairing_required: true,
      credentials_persisted: false,
      modes: ["publish_ready", "safe_sharing", "video_rescue", "useful_content", "advanced_ai"],
    });
    vi.spyOn(connectorClient, "isPaired").mockReturnValue(true);
    vi.spyOn(connectorClient, "providers").mockResolvedValue([{
      profile_id: "my-ai",
      display_name: "My AI",
      provider_id: "custom-openai",
      protocol: "openai_compatible",
      api_base_url: "https://provider.example/v1",
      model_id: "user-paid-model",
      capabilities: ["structured_text"],
      request_json_object: false,
      credential_state: "memory_only",
    }]);

    render(<I18nProvider initialLocale="en"><ConnectorPage /></I18nProvider>);

    expect(await screen.findByText("Full local modes connected")).toBeVisible();
    for (const name of ["Publish Ready", "Safe Sharing", "Video Rescue", "Useful Content", "Advanced AI"]) {
      expect(screen.getByRole("link", { name: new RegExp(name) })).toHaveAttribute("href", expect.stringContaining("http://127.0.0.1:8765/"));
    }
    expect(screen.getByText("user-paid-model")).toBeVisible();
    expect(screen.queryByText("user-owned-test-secret")).not.toBeInTheDocument();
  });
});
