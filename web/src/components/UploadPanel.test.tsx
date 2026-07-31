import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DetectorManifest } from "../types";
import { UploadPanel } from "./UploadPanel";

const OPTIONAL: DetectorManifest = {
  id: "prompt_alignment",
  display_name: "Prompt alignment",
  version: "1.0.0",
  description: "Optional local similarity diagnostic.",
  default_enabled: false,
  requires_prompt: true,
  requires_gpu: false,
  requires_network: false,
  optional_packages: ["open-clip-torch"],
  estimated_cost: "high",
  category: "ai",
  available: false,
  unavailable_reason: "Install genvideoscope[ai].",
};

it("explains why an optional detector cannot be selected", () => {
  render(
    <UploadPanel
      detectors={[OPTIONAL]}
      loadingDetectors={false}
      initialError={null}
      onSubmit={vi.fn()}
    />,
  );
  expect(screen.getByText(/Not installed/)).toHaveTextContent(
    "Install genvideoscope[ai]",
  );
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(screen.getByRole("button", { name: /Start local analysis/i })).toBeDisabled();
});
