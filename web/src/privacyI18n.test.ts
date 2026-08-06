import { expect, it } from "vitest";
import {
  privacyArtifactText,
  privacyCheckText,
  privacyIdentifierText,
  privacyServerText,
  privacyStatusText,
} from "./privacyI18n";

const PRODUCTION_PROFILE_LABELS = [
  ["public", "公开发布"],
  ["work_client", "工作或客户"],
  ["school", "学校或教育"],
  ["family", "家人和朋友"],
  ["external_ai", "外部 AI 服务"],
] as const;

const PRODUCTION_CHECK_LABELS = [
  ["decodable", "可解码检查：通过"],
  ["duration", "时长一致性检查：通过"],
  ["streams", "音视频流检查：通过"],
  ["profile", "分享对象配置检查：通过"],
  ["metadata", "元数据移除检查：通过"],
  ["visual_coverage", "视觉脱敏覆盖检查：通过"],
  ["qr_redaction", "二维码脱敏检查：通过"],
  ["text_redaction", "文字脱敏检查：通过"],
  ["audio_mute", "音频静音检查：通过"],
  ["black_regression", "黑屏回归检查：通过"],
  ["freeze_regression", "卡帧回归检查：通过"],
  ["public_artifact_privacy", "公开产物隐私检查：通过"],
] as const;

it("localizes known Safe Sharing lifecycle and machine identifiers", () => {
  expect(privacyStatusText("scanning", "zh-CN")).toBe("正在本地扫描隐私风险");
  expect(privacyIdentifierText("profile", "family", "zh-CN")).toBe("家人和朋友");
  expect(privacyIdentifierText("risk", "face_region", "zh-CN")).toBe("疑似人脸区域");
  expect(privacyIdentifierText("action", "visual_redaction", "zh-CN")).toBe("视觉区域脱敏");
  expect(privacyCheckText("decodable", "passed", "zh-CN")).toBe("可解码检查：通过");
  expect(privacyArtifactText("share-safe.mp4", "zh-CN")).toBe("已验证的本地分享副本");
});

it("uses a Chinese conservative fallback plus machine identifier for unknown server prose", () => {
  const text = privacyServerText(
    "Untranslated scanner detail in English",
    "zh-CN",
    "scanner_warning",
    "suspicious_text",
  );
  expect(text).toBe("扫描器提示需要人工复核（suspicious_text）");
  expect(text).not.toContain("Untranslated");
  expect(
    privacyServerText("Native detail", "en", "scanner_warning", "scanner"),
  ).toBe("Native detail");
});

it.each(PRODUCTION_PROFILE_LABELS)(
  "localizes production profile %s without exposing a raw identifier as its label",
  (profileId, expected) => {
    expect(privacyIdentifierText("profile", profileId, "zh-CN")).toBe(expected);
  },
);

it.each(PRODUCTION_CHECK_LABELS)(
  "localizes production verification check %s",
  (checkId, expected) => {
    expect(privacyCheckText(checkId, "passed", "zh-CN")).toBe(expected);
  },
);

it("uses neutral Chinese labels for unknown machine identifiers", () => {
  expect(privacyIdentifierText("profile", "future_profile", "zh-CN")).toBe(
    "其他分享对象",
  );
  const check = privacyCheckText("future_check", "passed", "zh-CN");
  expect(check).toBe("其他验证检查：通过");
  expect(check).not.toContain("future_check");
});
