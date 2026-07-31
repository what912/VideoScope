import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { AppProviders } from "../../app/AppProviders";
import { createDemoReport } from "../../data/demo-report";
import { FakeAuthClient } from "../../services/auth";
import {
  FakeShareClient,
  MemoryShareRecordStore,
} from "../../services/share";
import type { ShareRecordStore } from "../../services/share";
import type { Locale } from "../../i18n/types";
import { ShareDialog } from "./ShareDialog";

function renderDialog({
  authenticated = true,
  enabled = true,
  locale = "en",
  shareClient = new FakeShareClient(),
  shareRecordStore = new MemoryShareRecordStore(),
  online,
}: {
  authenticated?: boolean;
  enabled?: boolean;
  locale?: Locale;
  shareClient?: FakeShareClient;
  shareRecordStore?: ShareRecordStore;
  online?: boolean;
} = {}) {
  const authClient = new FakeAuthClient({
    initialSession: authenticated
      ? {
          user: {
            email: "reviewer@example.test",
            id: "owner-123",
          },
        }
      : null,
  });
  const report = createDemoReport(locale);
  report.prompt = "A lighthouse in a storm";
  const view = render(
    <AppProviders authClient={authClient} initialLocale={locale} online={online}>
      <ShareDialog
        onClose={() => undefined}
        report={report}
        shareClient={shareClient}
        shareEnabled={enabled}
        shareRecordStore={shareRecordStore}
      />
    </AppProviders>,
  );
  return { authClient, shareClient, shareRecordStore, view };
}

describe("ShareDialog", () => {
  it("keeps sharing offline without calling the remote client and recovers online", async () => {
    const first = renderDialog({ online: false });
    const { shareClient } = first;

    expect(
      await screen.findByRole("status", { name: "Sharing is offline" }),
    ).toHaveTextContent(
      "Reconnect before creating or revoking public links. Local reports remain available.",
    );
    expect(
      screen.queryByRole("button", { name: "Create share link" }),
    ).not.toBeInTheDocument();
    expect(shareClient.requests).toHaveLength(0);

    first.view.unmount();
    renderDialog({ online: true, shareClient });
    expect(
      await screen.findByRole("button", { name: "Create share link" }),
    ).toBeDisabled();
    expect(shareClient.requests).toHaveLength(0);
  });

  it("does not call the client until the final consent is checked", async () => {
    const { shareClient } = renderDialog();
    const createButton = await screen.findByRole("button", {
      name: "Create share link",
    });

    expect(createButton).toBeDisabled();
    fireEvent.click(createButton);
    expect(shareClient.requests).toHaveLength(0);

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /I understand that the listed data will leave this device/i,
      }),
    );
    fireEvent.click(createButton);

    await waitFor(() => expect(shareClient.requests).toHaveLength(1));
  });

  it("lists exact outgoing fields, keeps prompt separate, and selects evidence explicitly", async () => {
    const { shareClient } = renderDialog();

    expect(
      await screen.findByText("Data leaving this device"),
    ).toBeInTheDocument();
    expect(screen.getByText("Report schema and tool version")).toBeInTheDocument();
    expect(screen.getByText("Video dimensions and duration")).toBeInTheDocument();
    expect(screen.getByText("Detector results and limitations")).toBeInTheDocument();
    expect(screen.getByText("Report creation time")).toBeInTheDocument();
    expect(
      screen.getByText("Public title, when provided"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Prompt, only when separately selected"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Video MIME type, file size, frame rate, and audio flag"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Detector configuration, execution timing, and sanitized errors"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Detector-local metrics, summary counts, and warnings"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Sanitized runtime and selected evidence metadata"),
    ).toBeInTheDocument();
    expect(screen.getByText("No original video or evidence image files")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Public report title (optional)"), {
      target: { value: "Team review" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Include prompt/i }));
    const evidenceCheckbox = screen.getAllByRole("checkbox", {
      name: /Evidence at/i,
    })[0];
    fireEvent.click(evidenceCheckbox);
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /I understand that the listed data will leave this device/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create share link" }));

    await waitFor(() => expect(shareClient.requests).toHaveLength(1));
    const request = shareClient.requests[0];
    expect(request.ownerId).toBe("owner-123");
    expect(request.report.title).toBe("Team review");
    expect(request.report.prompt).toBeDefined();
    expect(
      request.report.findings.reduce(
        (count, finding) => count + finding.evidence.length,
        0,
      ),
    ).toBe(1);
  });

  it("shows Not configured and performs zero client calls unless every gate is ready", () => {
    const disabledClient = new FakeShareClient();
    renderDialog({ enabled: false, shareClient: disabledClient });
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create share link" })).not.toBeInTheDocument();
    expect(disabledClient.requests).toHaveLength(0);
  });

  it("requires an authenticated session and stays bilingual", async () => {
    const signedOutClient = new FakeShareClient();
    renderDialog({
      authenticated: false,
      locale: "zh-CN",
      shareClient: signedOutClient,
    });

    await waitFor(() => {
      expect(screen.getByText("未配置")).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: "创建分享链接" }),
    ).not.toBeInTheDocument();
    expect(signedOutClient.requests).toHaveLength(0);
  });

  it("revokes a just-created link through the owner client", async () => {
    const { shareClient } = renderDialog();
    fireEvent.click(
      await screen.findByRole("checkbox", {
        name: /I understand that the listed data will leave this device/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create share link" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Revoke link" }),
    );

    await waitFor(() =>
      expect(shareClient.revokedPublicIds).toEqual([
        "00000000-0000-4000-8000-000000000001",
      ]),
    );
    expect(screen.getByText("Share link revoked")).toBeInTheDocument();
  });

  it("restores a minimal local owner index after remount and can revoke from it", async () => {
    const shareClient = new FakeShareClient();
    const shareRecordStore = new MemoryShareRecordStore();
    const first = renderDialog({ shareClient, shareRecordStore });
    fireEvent.change(
      await screen.findByLabelText("Public report title (optional)"),
      { target: { value: "Team review" } },
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /I understand that the listed data will leave this device/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create share link" }));
    await screen.findByText("Sanitized share link created");
    first.view.unmount();

    renderDialog({ shareClient, shareRecordStore });
    expect(
      await screen.findByRole("heading", { name: "Saved share links" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/This list is not synchronized across devices/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText("http://localhost:3000/report/00000000-0000-4000-8000-000000000001?shared=1"),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Revoke Team review",
      }),
    );

    await waitFor(() =>
      expect(shareClient.revokedPublicIds).toContain(
        "00000000-0000-4000-8000-000000000001",
      ),
    );
    expect(
      screen.queryByRole("button", { name: "Revoke Team review" }),
    ).not.toBeInTheDocument();
  });

  it("moves focus after create and revoke replace the active controls", async () => {
    renderDialog();
    fireEvent.click(
      await screen.findByRole("checkbox", {
        name: /I understand that the listed data will leave this device/i,
      }),
    );
    const create = screen.getByRole("button", { name: "Create share link" });
    create.focus();
    fireEvent.click(create);

    const copyLink = await screen.findByRole("button", { name: "Copy link" });
    await waitFor(() => expect(copyLink).toHaveFocus());
    const revoke = screen.getByRole("button", { name: "Revoke link" });
    revoke.focus();
    fireEvent.keyDown(
      screen.getByRole("dialog", { name: "Share a sanitized report" }),
      { key: "Tab" },
    );
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    revoke.focus();
    fireEvent.click(revoke);
    await screen.findByText("Share link revoked");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close" })).toHaveFocus(),
    );
  });

  it("keeps a newly created link actionable when the local index cannot persist", async () => {
    const unavailableStore: ShareRecordStore = {
      list: async () => [],
      put: async () => {
        throw new Error("quota");
      },
      remove: async () => undefined,
    };
    const { shareClient } = renderDialog({
      shareRecordStore: unavailableStore,
    });
    fireEvent.click(
      await screen.findByRole("checkbox", {
        name: /I understand that the listed data will leave this device/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create share link" }));

    expect(
      await screen.findByRole("button", { name: "Copy link" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/local revoke shortcut could not be saved/i),
    ).toBeInTheDocument();
    expect(shareClient.requests).toHaveLength(1);
  });

  it("traps focus, closes on Escape, and restores focus to the trigger", async () => {
    const authClient = new FakeAuthClient({
      initialSession: { user: { id: "owner-123" } },
    });
    const shareClient = new FakeShareClient();

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <AppProviders authClient={authClient} initialLocale="en">
          <button onClick={() => setOpen(true)} type="button">
            Open sharing
          </button>
          {open ? (
            <ShareDialog
              onClose={() => setOpen(false)}
              report={createDemoReport("en")}
              shareClient={shareClient}
              shareEnabled
            />
          ) : null}
        </AppProviders>
      );
    }

    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open sharing" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", {
      name: "Share a sanitized report",
    });
    const close = screen.getByRole("button", { name: "Close" });
    await waitFor(() => expect(close).toHaveFocus());
    const consent = await screen.findByRole("checkbox", {
      name: /I understand that the listed data will leave this device/i,
    });
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(consent).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(close).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(
      screen.queryByRole("dialog", { name: "Share a sanitized report" }),
    ).not.toBeInTheDocument();
  });

  it("contains clipboard rejection and reports a copy failure", async () => {
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async () => Promise.reject(new Error("denied")) },
    });
    const { shareClient } = renderDialog();
    fireEvent.click(
      await screen.findByRole("checkbox", {
        name: /I understand that the listed data will leave this device/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create share link" }));
    fireEvent.click(await screen.findByRole("button", { name: "Copy link" }));

    await waitFor(() =>
      expect(screen.getByText("Copy failed. Select the link manually.")).toBeInTheDocument(),
    );
    expect(shareClient.requests).toHaveLength(1);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
  });
});
