import type { Locale } from "../../i18n/types";
import type { PublicRecoveryStateId } from "./recovery-states";

interface StaticCopy {
  privacy: {
    eyebrow: string;
    title: string;
    introduction: string;
    localTitle: string;
    localBody: string;
    storageTitle: string;
    storageBody: string;
    urlTitle: string;
    urlBody: string;
    accountTitle: string;
    accountBody: string;
    sharingTitle: string;
    sharingBody: string;
    controlsTitle: string;
    controlsBody: string;
    controlsAction: string;
    usageLoading: string;
    usageUnavailable: string;
    reportSingular: string;
    reportPlural: string;
    thumbnailSingular: string;
    thumbnailPlural: string;
    shareLinkSingular: string;
    shareLinkPlural: string;
    bytesStored: string;
    shareBytesStored: string;
    deleteAction: string;
    deleteConfirmTitle: string;
    deleteConfirmBody: string;
    deleteConfirmAction: string;
    deleteCancel: string;
    deleteSuccess: string;
    deleteFailure: string;
    deletePartial: string;
  };
  docs: {
    eyebrow: string;
    title: string;
    introduction: string;
    matrixLabel: string;
    capability: string;
    browser: string;
    desktop: string;
    available: string;
    desktopOnly: string;
    capabilityNames: Record<CapabilityId, string>;
    browserPreview: string;
    browserPreviewBody: string;
    privacyBoundary: string;
    privacyBoundaryBody: string;
    troubleshooting: string;
    recovery: Record<
      PublicRecoveryStateId,
      { title: string; action: string }
    >;
  };
  notFound: {
    eyebrow: string;
    title: string;
    description: string;
    action: string;
  };
}

export type CapabilityId =
  | "browser_cpu_detectors"
  | "local_reports"
  | "ffmpeg_probe"
  | "benchmark"
  | "ai_providers"
  | "ocr"
  | "web_api";

const en: StaticCopy = {
  privacy: {
    eyebrow: "PRIVACY · LOCAL-FIRST",
    title: "Privacy by default",
    introduction:
      "VideoScope starts with anonymous, on-device analysis. Optional network features remain separate and explicit.",
    localTitle: "Local video handling",
    localBody:
      "A selected file is decoded in this browser. The original video is not uploaded by anonymous analysis, and temporary object URLs are released when the session ends.",
    storageTitle: "What this browser stores",
    storageBody:
      "Saved IndexedDB reports contain metadata, detector configuration, Findings, compact evidence thumbnails, and review state. localStorage keeps a minimal share-link index with public ID, local report ID, title, creation time, and optional expiry so signed-in owners can manage links in this browser. Neither store contains the original video.",
    urlTitle: "Direct URL disclosure",
    urlBody:
      "After consent, the browser contacts the exact HTTPS source you enter. That source host can observe your IP address and request metadata. VideoScope does not proxy the request.",
    accountTitle: "Optional account data",
    accountBody:
      "When Supabase is configured and you choose to sign in, the provider stores the account session and profile fields needed for authentication. Account deletion is not available until a private, verified request channel and deletion process are published and tested, so production sign-in must remain disabled. Local analysis remains available without an account.",
    sharingTitle: "Optional sanitized sharing",
    sharingBody:
      "Sharing is disabled unless configured. A final consent screen lists the sanitized report fields and selected evidence records that will leave the device. The original video and evidence image files are not shared.",
    controlsTitle: "Delete local data",
    controlsBody:
      "Delete saved reports, evidence thumbnails, review state, the active local video session, and every VideoScope share-link index from this browser.",
    controlsAction: "Open local data controls",
    usageLoading: "Reading local storage usage…",
    usageUnavailable: "Local storage usage is unavailable.",
    reportSingular: "saved report",
    reportPlural: "saved reports",
    thumbnailSingular: "thumbnail",
    thumbnailPlural: "thumbnails",
    shareLinkSingular: "saved share link",
    shareLinkPlural: "saved share links",
    bytesStored: "stored locally",
    shareBytesStored: "in the local share index",
    deleteAction: "Delete all local data",
    deleteConfirmTitle: "Delete all local browser data?",
    deleteConfirmBody:
      "This removes every saved report, evidence thumbnail, review state, active video session, and local share-link index from this browser. It does not revoke an already published remote link. This action cannot be undone.",
    deleteConfirmAction: "Confirm deletion",
    deleteCancel: "Cancel",
    deleteSuccess: "Local browser data deleted.",
    deleteFailure:
      "Local data could not be deleted. Existing reports remain available.",
    deletePartial: "Some local data could not be deleted.",
  },
  docs: {
    eyebrow: "DOCS · TWO WORKFLOWS",
    title: "Choose the right VideoScope workflow",
    introduction:
      "The browser is a fast local preview. The desktop package is the reproducible FFmpeg-based diagnostic and research workflow.",
    matrixLabel: "Capability matrix",
    capability: "Capability",
    browser: "Browser preview",
    desktop: "Desktop",
    available: "Available",
    desktopOnly: "Desktop",
    capabilityNames: {
      browser_cpu_detectors: "Four browser CPU detectors",
      local_reports: "Local JSON and printable report",
      ffmpeg_probe: "FFmpeg probing",
      benchmark: "Benchmark",
      ai_providers: "AI providers",
      ocr: "OCR",
      web_api: "Web API",
    },
    browserPreview: "Browser preview",
    browserPreviewBody:
      "The public site samples once and runs four bounded CPU heuristics: near-black, possible freeze, scene-relative blur, and global luminance flicker. It does not inspect every encoded frame.",
    privacyBoundary: "Optional network boundaries",
    privacyBoundaryBody:
      "Local files stay on device. Direct URL import contacts the entered host after consent. Sign-in and sanitized sharing stay unavailable when their optional services are not configured or the browser is offline.",
    troubleshooting: "Troubleshooting",
    recovery: {
      missing_file: {
        title: "No file selected",
        action: "Choose a supported local video before starting analysis.",
      },
      unsupported_media: {
        title: "Unsupported media",
        action: "Try MP4, WebM, MOV, or MKV, or use the desktop FFmpeg workflow.",
      },
      decode_failure: {
        title: "Browser decode failed",
        action: "Try another browser-supported encode or analyze it with the desktop CLI.",
      },
      duration_unavailable: {
        title: "Duration unavailable",
        action: "Remux the file or use desktop ffprobe to inspect its metadata.",
      },
      file_too_large: {
        title: "File exceeds the public limit",
        action: "Choose a file at or below 500 MiB or use the desktop CLI.",
      },
      canvas_unavailable: {
        title: "Canvas analysis unavailable",
        action: "Enable browser canvas support or use the desktop workflow.",
      },
      memory_or_sample_cap: {
        title: "Memory or sample cap reached",
        action: "Use Quick Scan, close memory-heavy tabs, or move to the desktop CLI.",
      },
      cors_failure: {
        title: "Direct URL blocked by CORS",
        action: "Download the video and choose it locally instead.",
      },
      cancelled: {
        title: "Analysis cancelled",
        action: "Select the file again when you are ready; temporary resources were released.",
      },
      detector_failure: {
        title: "One detector failed",
        action: "Review the other results and the separate detector error before retrying.",
      },
      no_findings: {
        title: "No Findings",
        action: "Treat this as no intervals from the enabled heuristics, not proof of perfect quality.",
      },
      local_report_missing: {
        title: "Local report missing",
        action: "Start a new analysis or open another report stored in this browser.",
      },
      shared_report_unavailable: {
        title: "Shared report revoked or expired",
        action: "Ask the owner for a current link; no fallback demo is substituted.",
      },
      auth_unavailable: {
        title: "Authentication unavailable",
        action: "Continue anonymously; local analysis does not require sign-in.",
      },
      optional_service_offline: {
        title: "Optional service offline",
        action: "Reconnect before sign-in or sharing, or continue with local analysis.",
      },
    },
  },
  notFound: {
    eyebrow: "404 · OBSERVATION ENDED",
    title: "This route is outside the observatory",
    description:
      "The requested page is not part of this VideoScope build. No report or demo was substituted.",
    action: "Return home",
  },
};

const zhCN: StaticCopy = {
  privacy: {
    eyebrow: "隐私 · 本地优先",
    title: "默认保护隐私",
    introduction:
      "VideoScope 默认匿名并在设备内完成分析。可选联网功能彼此独立，并且只有在你明确操作后才会启用。",
    localTitle: "本地视频处理",
    localBody:
      "所选文件只在此浏览器中解码。匿名分析不会上传原始视频；会话结束时会释放临时对象 URL。",
    storageTitle: "此浏览器保存什么",
    storageBody:
      "IndexedDB 中保存的报告包含元数据、检测器配置、Findings、压缩证据缩略图和复核状态，不包含原始视频。本地存储还会保留最小化的分享链接索引。",
    urlTitle: "直接 URL 的网络披露",
    urlBody:
      "在你同意后，浏览器会联系你输入的 HTTPS 来源。来源主机可以观察你的 IP 地址和请求元数据；VideoScope 不会代理该请求。",
    accountTitle: "可选账户数据",
    accountBody:
      "配置 Supabase 且你主动登录后，服务商会保存身份验证所需的账户会话与资料字段。在私密且可验证的申请渠道与删除流程发布并测试前，暂不提供账户删除，因此生产登录必须保持关闭；本地分析始终可以不登录使用。",
    sharingTitle: "可选脱敏分享",
    sharingBody:
      "分享功能默认关闭。最终确认界面会逐项列出将离开设备的脱敏报告字段和所选证据记录；不会分享原始视频或证据图片文件。",
    controlsTitle: "删除本地数据",
    controlsBody:
      "从此浏览器删除已保存报告、证据缩略图、复核状态、当前视频会话和全部 VideoScope 分享链接索引。",
    controlsAction: "打开本地数据控件",
    usageLoading: "正在读取本地存储用量…",
    usageUnavailable: "无法读取本地存储用量。",
    reportSingular: "份已保存报告",
    reportPlural: "份已保存报告",
    thumbnailSingular: "张缩略图",
    thumbnailPlural: "张缩略图",
    shareLinkSingular: "个已保存分享链接",
    shareLinkPlural: "个已保存分享链接",
    bytesStored: "存储于本地",
    shareBytesStored: "用于本地分享索引",
    deleteAction: "删除全部本地数据",
    deleteConfirmTitle: "删除全部浏览器本地数据？",
    deleteConfirmBody:
      "这会从当前浏览器删除全部已保存报告、证据缩略图、复核状态、活动视频会话和本地分享链接索引，但不会撤销已经发布到远程服务的链接。此操作无法撤销。",
    deleteConfirmAction: "确认删除",
    deleteCancel: "取消",
    deleteSuccess: "浏览器本地数据已删除。",
    deleteFailure: "无法删除本地数据，现有报告仍然可用。",
    deletePartial: "部分本地数据无法删除。",
  },
  docs: {
    eyebrow: "文档 · 两种工作流",
    title: "选择适合的 VideoScope 工作流",
    introduction:
      "浏览器适合快速本地预览；桌面软件包是基于 FFmpeg、可复现的诊断与研究工作流。",
    matrixLabel: "能力对照表",
    capability: "能力",
    browser: "浏览器预览",
    desktop: "桌面端",
    available: "可用",
    desktopOnly: "仅桌面端",
    capabilityNames: {
      browser_cpu_detectors: "四个浏览器 CPU 检测器",
      local_reports: "本地 JSON 和可打印报告",
      ffmpeg_probe: "FFmpeg 探测",
      benchmark: "基准评测",
      ai_providers: "AI 模型提供器",
      ocr: "OCR 文字识别",
      web_api: "Web API",
    },
    browserPreview: "浏览器预览",
    browserPreviewBody:
      "公开网站只抽样一次，并运行四种有界 CPU 启发式检测：近黑、疑似冻结、场景相对模糊和全局亮度闪烁。它不会检查每一个编码帧。",
    privacyBoundary: "可选联网边界",
    privacyBoundaryBody:
      "本地文件留在设备上。直接 URL 导入仅在同意后联系输入的主机；未配置服务或浏览器离线时，登录与脱敏分享保持不可用。",
    troubleshooting: "故障排查",
    recovery: {
      missing_file: { title: "尚未选择文件", action: "开始分析前请选择受支持的本地视频。" },
      unsupported_media: { title: "不支持的媒体", action: "尝试 MP4、WebM、MOV 或 MKV，或使用桌面 FFmpeg 工作流。" },
      decode_failure: { title: "浏览器解码失败", action: "尝试浏览器支持的编码，或改用桌面 CLI 分析。" },
      duration_unavailable: { title: "无法读取时长", action: "重新封装文件，或使用桌面 ffprobe 检查元数据。" },
      file_too_large: { title: "文件超过公开限制", action: "请选择不超过 500 MiB 的文件，或使用桌面 CLI。" },
      canvas_unavailable: { title: "画布分析不可用", action: "启用浏览器画布支持，或使用桌面工作流。" },
      memory_or_sample_cap: { title: "达到内存或抽样上限", action: "使用快速扫描、关闭占用内存的标签页，或改用桌面 CLI。" },
      cors_failure: { title: "直接 URL 被 CORS 阻止", action: "请下载视频后从本地选择。" },
      cancelled: { title: "分析已取消", action: "准备好后重新选择文件；临时资源已释放。" },
      detector_failure: { title: "某个检测器失败", action: "先复核其他结果和单独显示的检测器错误，再决定是否重试。" },
      no_findings: { title: "没有 Findings", action: "这只表示启用的启发式没有返回区间，不代表视频质量完美。" },
      local_report_missing: { title: "本地报告不存在", action: "请重新分析，或打开此浏览器中保存的其他报告。" },
      shared_report_unavailable: { title: "分享报告已撤销或过期", action: "请向所有者获取有效链接；页面不会用演示数据替代。" },
      auth_unavailable: { title: "身份验证不可用", action: "请继续匿名使用；本地分析不要求登录。" },
      optional_service_offline: { title: "可选服务离线", action: "联网后再登录或分享，也可以继续本地分析。" },
    },
  },
  notFound: {
    eyebrow: "404 · 观测结束",
    title: "此路径不在观测站内",
    description:
      "当前 VideoScope 构建不包含所请求页面，也没有用其他报告或演示数据替代。",
    action: "返回首页",
  },
};

export function getStaticCopy(locale: Locale): StaticCopy {
  return locale === "zh-CN" ? zhCN : en;
}
