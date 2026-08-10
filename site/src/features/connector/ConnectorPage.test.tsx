import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../../i18n/I18nProvider";
import { connectorClient } from "../../services/connector/connector-client";
import { ConnectorPage } from "./ConnectorPage";

const readyStatus = {
  status: "ready",
  service: "VideoScope Local Connector",
  version: "0.8.0",
  pairing_required: true,
  credentials_persisted: false,
  modes: ["publish_ready", "safe_sharing", "video_rescue", "useful_content", "advanced_ai"],
  ffmpeg_available: true,
  ffprobe_available: true,
};

describe("ConnectorPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("gives a zero-experience Chinese user a complete numbered setup path", async () => {
    vi.spyOn(connectorClient, "status").mockRejectedValue(new TypeError("offline"));
    vi.spyOn(connectorClient, "isPaired").mockReturnValue(false);

    render(<I18nProvider initialLocale="zh-CN"><ConnectorPage /></I18nProvider>);

    expect(await screen.findByText("从下载到得到第一个结果，一步一步来。")).toBeVisible();
    expect(screen.getByRole("heading", { name: "下载 Windows 版 VideoScope" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "双击 VideoScope-Setup-x64.exe" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "启动 VideoScope 连接器" })).toBeVisible();
    expect(screen.getByRole("button", { name: "重新检查连接器" })).toBeEnabled();
    expect(screen.getByText("约 3 分钟 · 每台电脑只做一次")).toBeVisible();
    expect(screen.getByText("不会创建云账户、上传视频或自动安装 AI 模型。")).toBeVisible();
  });

  it("explains the exact pairing-code location and pairs without preserving whitespace", async () => {
    vi.spyOn(connectorClient, "status").mockResolvedValue(readyStatus);
    vi.spyOn(connectorClient, "isPaired").mockReturnValue(false);
    const pair = vi.spyOn(connectorClient, "pair").mockResolvedValue();
    vi.spyOn(connectorClient, "providers").mockResolvedValue([]);

    render(<I18nProvider initialLocale="en"><ConnectorPage /></I18nProvider>);

    const input = await screen.findByLabelText("Connector pairing code");
    expect(screen.getByText("VideoScope Local Connector pairing code:")).toBeVisible();
    fireEvent.change(input, { target: { value: "  Ab1_cdEF2345  " } });
    fireEvent.click(screen.getByRole("button", { name: "Pair this browser" }));

    await waitFor(() => expect(pair).toHaveBeenCalledWith("Ab1_cdEF2345"));
    expect(await screen.findByRole("link", { name: "Choose a video and start" })).toHaveAttribute(
      "href",
      "http://127.0.0.1:8765/?mode=analyze",
    );
  });

  it("exposes every full local workflow only after a paired connector responds", async () => {
    vi.spyOn(connectorClient, "status").mockResolvedValue(readyStatus);
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
    expect(screen.getByRole("link", { name: "Choose a video and start" })).toBeVisible();
    for (const name of ["Publish Ready", "Safe Sharing", "Video Rescue", "Useful Content", "Advanced AI"]) {
      expect(screen.getByRole("link", { name: new RegExp(name) })).toHaveAttribute("href", expect.stringContaining("http://127.0.0.1:8765/"));
    }
    expect(screen.getByText("user-paid-model")).toBeVisible();
    expect(screen.queryByText("user-owned-test-secret")).not.toBeInTheDocument();
  });

  it("does not claim full readiness when FFmpeg is missing", async () => {
    vi.spyOn(connectorClient, "status").mockResolvedValue({
      ...readyStatus,
      status: "degraded",
      ffmpeg_available: false,
      ffprobe_available: false,
    });
    vi.spyOn(connectorClient, "isPaired").mockReturnValue(true);
    vi.spyOn(connectorClient, "providers").mockResolvedValue([]);

    render(<I18nProvider initialLocale="en"><ConnectorPage /></I18nProvider>);

    expect(await screen.findByRole("alert")).toHaveTextContent("FFmpeg still needs attention");
  });
});
