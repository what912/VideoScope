import type {
  PrivacyJobStatus,
  PrivacyRiskType,
  RedactionStyle,
} from "./types";

export type PrivacyLocale = "en" | "zh-CN";

const STATUS_ZH: Record<PrivacyJobStatus, string> = {
  queued: "安全分享任务已排队",
  inspecting: "正在检查本地源视频",
  scanning: "正在本地扫描隐私风险",
  awaiting_review: "等待人工复核隐私风险",
  planning: "正在生成已复核的隐私计划",
  previewing: "正在生成私有预览",
  awaiting_confirmation: "等待确认精确处理计划",
  processing: "正在生成确认后的分享副本",
  verifying: "正在验证独立分享包",
  completed: "安全分享包已完成",
  needs_review: "安全分享包需要人工复核",
  partial: "安全分享包存在未解决的不确定性",
  failed: "安全分享任务失败",
  cancelled: "安全分享任务已取消",
};

const STATUS_EN: Record<PrivacyJobStatus, string> = {
  queued: "Safe Sharing task queued",
  inspecting: "Inspecting the local source",
  scanning: "Scanning for privacy risks locally",
  awaiting_review: "Awaiting human privacy review",
  planning: "Building the reviewed privacy plan",
  previewing: "Rendering the private review preview",
  awaiting_confirmation: "Awaiting exact plan confirmation",
  processing: "Rendering the confirmed sharing copy",
  verifying: "Verifying the isolated share package",
  completed: "Safe Sharing package completed",
  needs_review: "Safe Sharing package needs human review",
  partial: "Safe Sharing package has unresolved uncertainty",
  failed: "Safe Sharing task failed",
  cancelled: "Safe Sharing task cancelled",
};

const PROFILE_ZH: Record<string, string> = {
  public: "公开发布",
  work_client: "工作或客户",
  school: "学校或教育",
  family: "家人和朋友",
  external_ai: "外部 AI 服务",
};

const RISK_ZH: Record<PrivacyRiskType, string> = {
  metadata: "敏感元数据",
  face_region: "疑似人脸区域",
  qr_code: "二维码或条形码",
  barcode: "条形码",
  suspicious_text: "疑似敏感文字",
  manual_visual: "人工视觉区域",
  manual_audio: "人工静音区间",
};

const ACTION_ZH: Record<string, string> = {
  remove_metadata: "移除元数据",
  crop: "裁切画面",
  visual_redaction: "视觉区域脱敏",
  audio_mute: "音频静音",
  remux: "重新封装",
  verify: "验证输出",
};

const CHECK_ZH: Record<string, string> = {
  decodable: "可解码检查",
  duration: "时长一致性检查",
  streams: "音视频流检查",
  profile: "分享对象配置检查",
  metadata: "元数据移除检查",
  visual_coverage: "视觉脱敏覆盖检查",
  qr_redaction: "二维码脱敏检查",
  text_redaction: "文字脱敏检查",
  audio_mute: "音频静音检查",
  black_regression: "黑屏回归检查",
  freeze_regression: "卡帧回归检查",
  public_artifact_privacy: "公开产物隐私检查",
};

const CHECK_STATUS_ZH: Record<string, string> = {
  passed: "通过",
  needs_review: "需要复核",
  failed: "失败",
};

const ARTIFACT_ZH: Record<string, string> = {
  "share-safe.mp4": "已验证的本地分享副本",
  "privacy-summary.json": "公开隐私摘要",
  "changes.json": "处理变更记录",
  "verification.json": "验证结果",
  "technical-report.json": "公开技术报告",
  "manifest.json": "分享包清单",
};

const ARTIFACT_EN: Record<string, string> = {
  "share-safe.mp4": "Verified local sharing copy",
  "privacy-summary.json": "Public privacy summary",
  "changes.json": "Applied changes record",
  "verification.json": "Verification results",
  "technical-report.json": "Public technical report",
  "manifest.json": "Share package manifest",
};

const STYLE_ZH: Record<RedactionStyle, string> = {
  blur: "模糊",
  pixelate: "像素化",
  solid_fill: "纯色遮挡",
  crop: "裁切",
  mute: "静音",
  remove_metadata: "移除元数据",
};

export function privacyStatusText(
  status: PrivacyJobStatus,
  locale: PrivacyLocale,
): string {
  return locale === "zh-CN" ? STATUS_ZH[status] : STATUS_EN[status];
}

export function privacyIdentifierText(
  kind: "profile" | "risk" | "action" | "style",
  identifier: string,
  locale: PrivacyLocale,
): string {
  if (locale === "en") return identifier.replaceAll("_", " ");
  if (kind === "profile") return PROFILE_ZH[identifier] ?? "其他分享对象";
  if (kind === "risk") return RISK_ZH[identifier as PrivacyRiskType] ?? "其他隐私风险";
  if (kind === "action") return ACTION_ZH[identifier] ?? "其他本地处理操作";
  return STYLE_ZH[identifier as RedactionStyle] ?? "其他处理方式";
}

export function privacyCheckText(
  checkId: string,
  status: string,
  locale: PrivacyLocale,
): string {
  if (locale === "en") return `${checkId.replaceAll("_", " ")}: ${status.replaceAll("_", " ")}`;
  const check = CHECK_ZH[checkId] ?? "其他验证检查";
  const outcome = CHECK_STATUS_ZH[status] ?? `状态（${status}）`;
  return `${check}：${outcome}`;
}

export function privacyArtifactText(
  relativePath: string,
  locale: PrivacyLocale,
): string {
  const name = relativePath.split("/").at(-1) ?? relativePath;
  if (locale === "en") return ARTIFACT_EN[name] ?? name.replaceAll("-", " ");
  return ARTIFACT_ZH[name] ?? `分享包产物（${name}）`;
}

export function privacyServerText(
  serverText: string | null | undefined,
  locale: PrivacyLocale,
  kind: "scanner_warning" | "risk_description" | "verification" | "error",
  machineId: string,
): string {
  if (locale === "en") return serverText?.trim() || machineId.replaceAll("_", " ");
  const fallback: Record<typeof kind, string> = {
    scanner_warning: "扫描器提示需要人工复核",
    risk_description: "检测到需要人工判断的隐私线索",
    verification: "此验证项需要结合机器标识查看",
    error: "本地任务未能完成",
  };
  return `${fallback[kind]}（${machineId}）`;
}
