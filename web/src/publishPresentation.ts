import type {
  PublishAction,
  PublishActionKind,
  PublishJobStatus,
  VerificationCheck,
  VerificationStatus,
} from "./types";

export type PresentationLocale = "en" | "zh-CN";
export type PublishErrorKey = "workflow";

export interface PublishPresentationError {
  key: PublishErrorKey;
  detail: string | null;
}

interface ActionPresentation {
  label: string;
  description: string;
}

const ZH_STATUS_MESSAGE: Record<PublishJobStatus, string> = {
  queued: "任务已排队，等待本地处理。",
  inspecting: "正在检查本地源文件。",
  planning: "正在生成版本化处理计划。",
  awaiting_confirmation: "计划已就绪，等待明确确认。",
  processing: "已确认计划，正在本地处理。",
  verifying: "正在按所选 Profile 验证输出。",
  completed: "输出已通过全部版本化检查。",
  needs_review: "输出已生成，但需要人工复核。",
  failed: "本地发布处理失败。",
  cancelled: "本地发布任务已取消。",
};

const ZH_ACTION: Record<PublishActionKind, ActionPresentation> = {
  remux: {
    label: "重封装",
    description: "将兼容的音视频流重新封装为 MP4，不重新编码内容。",
  },
  transcode: {
    label: "转码",
    description: "按所选兼容 Profile 编码视频与音频流。",
  },
  scale_pad: {
    label: "缩放并留边",
    description: "完整保留源画面比例并留边适配目标画布，不进行裁剪。",
  },
  strip_metadata: {
    label: "清理元数据",
    description: "移除输出中的非必要源元数据。",
  },
  faststart: {
    label: "优化起播",
    description: "将 MP4 播放元数据移动到文件开头。",
  },
  extract_cover: {
    label: "提取封面",
    description: "提取一张代表性封面图。",
  },
};

const ZH_UNKNOWN_ACTION: ActionPresentation = {
  label: "本地处理步骤",
  description: "执行计划中声明的本地处理步骤。",
};

const ZH_VERIFICATION: Partial<
  Record<string, Partial<Record<VerificationStatus, string>>>
> = {
  decodable: {
    passed: "输出已通过独立解码探测。",
    failed: "输出无法通过独立解码探测。",
  },
  duration: {
    passed: "输出时长处于源文件相对容差内。",
    failed: "输出时长不可用或超出允许偏差。",
  },
  dimensions: {
    passed: "输出尺寸符合所选 Profile 画布。",
    failed: "输出尺寸不符合所选 Profile 画布。",
  },
  container: {
    passed: "输出容器符合所选 Profile。",
    failed: "输出容器不可用或不兼容。",
  },
  video_codec: {
    passed: "输出视频编码符合所选 Profile。",
    failed: "输出视频编码不可用或不兼容。",
  },
  pixel_format: {
    passed: "输出像素格式符合所选 Profile。",
    failed: "输出像素格式不可用或不兼容。",
  },
  frame_rate: {
    passed: "输出帧率处于所选 Profile 上限内。",
    failed: "输出帧率不可用、无效或超出 Profile 上限。",
  },
  audio_stream: {
    passed: "输出保留了源文件要求的音频流。",
    failed: "输出缺少源文件要求的音频流。",
  },
  audio_codec: {
    passed: "输出音频编码符合所选 Profile。",
    failed: "输出音频编码不可用或不兼容。",
  },
  near_black_regression: {
    passed: "处理后近黑高严重度区间未增加。",
    needs_review: "近黑区间对比需要人工复核。",
    failed: "近黑区间对比未通过。",
  },
  possible_freeze_regression: {
    passed: "处理后疑似冻结高严重度区间未增加。",
    needs_review: "疑似冻结区间对比需要人工复核。",
    failed: "疑似冻结区间对比未通过。",
  },
};

const ZH_UNKNOWN_VERIFICATION: Record<VerificationStatus, string> = {
  passed: "此项版本化技术检查已通过。",
  needs_review: "此项技术检查需要人工复核。",
  failed: "此项版本化技术检查未通过。",
};

const ERROR_FALLBACK: Record<
  PublishErrorKey,
  Record<PresentationLocale, string>
> = {
  workflow: {
    en: "Could not continue the local Publish Ready workflow.",
    "zh-CN": "无法继续本地发布就绪工作流。",
  },
};

export function presentPublishStatus(
  locale: PresentationLocale,
  status: PublishJobStatus,
  backendDetail: string,
): string {
  return locale === "zh-CN" ? ZH_STATUS_MESSAGE[status] : backendDetail;
}

export function presentPublishAction(
  locale: PresentationLocale,
  action: PublishAction,
): ActionPresentation {
  if (locale === "en") {
    return {
      label: action.kind.replaceAll("_", " "),
      description: action.description,
    };
  }
  return ZH_ACTION[action.kind] ?? ZH_UNKNOWN_ACTION;
}

export function presentVerificationCheck(
  locale: PresentationLocale,
  check: VerificationCheck,
): string {
  if (locale === "en") return check.message;
  return (
    ZH_VERIFICATION[check.check_id]?.[check.status] ??
    ZH_UNKNOWN_VERIFICATION[check.status]
  );
}

export function presentReviewReason(
  locale: PresentationLocale,
  reason: string,
  checks: VerificationCheck[],
): string {
  if (locale === "en") return reason;
  const matchingCheck = checks.find((check) => check.message === reason);
  return matchingCheck
    ? presentVerificationCheck(locale, matchingCheck)
    : ZH_UNKNOWN_VERIFICATION.needs_review;
}

export function presentBackendError(
  locale: PresentationLocale,
  backendDetail: string,
  chineseFallback: string,
): string {
  return locale === "zh-CN" ? chineseFallback : backendDetail;
}

export function capturePublishError(
  caught: unknown,
  key: PublishErrorKey = "workflow",
): PublishPresentationError {
  return {
    key,
    detail: caught instanceof Error ? caught.message : null,
  };
}

export function presentPublishError(
  locale: PresentationLocale,
  error: PublishPresentationError,
): string {
  if (locale === "zh-CN") return ERROR_FALLBACK[error.key][locale];
  return error.detail || ERROR_FALLBACK[error.key][locale];
}

export function presentPublishFailure(
  locale: PresentationLocale,
  backendDetail: string,
): string {
  return presentBackendError(
    locale,
    backendDetail,
    "本地处理未能完成，请查看技术日志。",
  );
}
