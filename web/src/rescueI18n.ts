export type RescueLocale = "en" | "zh-CN";

const text = {
  en: {
    creator: "what912", title: "Video Rescue", local: "Processed locally on this device. Your original file stays read-only.",
    choose: "Choose a video", strategy: "Rescue strategy", conservative: "Conservative", balanced: "Balanced", start: "Scan locally", symptoms: "What are you noticing?",
    progress: "Local rescue progress", plan: "Review exact plan", confirm: "Confirm exact local rescue plan", cancel: "Cancel task", delete: "Delete local task data",
    newTask: "New rescue task", damage: "Observable timeline", recoverable: "Decodable time is scan coverage, not a quality or recovery score.",
    previews: "Result comparison", original: "Source preview", faithful: "Faithful rescue", improved: "Improved viewing", unsupported: "No improved result was published.", verification: "verification", verificationPassed: "passed", verificationNeedsReview: "needs review", verificationFailed: "failed",
    downloadFaithful: "Download faithful rescue", downloadImproved: "Download improved viewing", downloadReport: "Download technical report", downloadJson: "Download JSON", openHtml: "Open HTML report",
    locked: "Protected source ranges", lockedStart: "Protected range start (seconds)", lockedEnd: "Protected range end (seconds)", addLocked: "Protect range", removeLocked: "Remove protected range", invalidLocked: "Enter an ordered non-negative protected range.", maximumStrength: "Maximum balanced improvement strength", advanced: "Advanced actions", processing: "Processing and verifying locally", completed: "Verified rescue ready", partial: "Partial recovery", needsReview: "Needs review", failed: "Rescue failed", cancelled: "Rescue cancelled", action: "Apply action", strength: "Signed strength", strategyConservative: "Preserve content and repair only structural playback issues.", strategyBalanced: "Add measured, bounded CPU improvements after a private preview.", timelineAria: "Observable damage and protected ranges timeline", coverage: "Observed decodable scan coverage", noScore: "This is measured time coverage, not a recovery or quality score.",
  },
  "zh-CN": {
    creator: "what912", title: "视频抢救", local: "仅在此设备本地处理。原始文件保持只读。",
    choose: "选择视频", strategy: "抢救策略", conservative: "保守", balanced: "平衡", start: "本地扫描", symptoms: "您观察到什么？",
    progress: "本地抢救进度", plan: "核对精确计划", confirm: "确认精确本地抢救计划", cancel: "取消任务", delete: "删除本地任务数据",
    newTask: "新建抢救任务", damage: "可观察时间轴", recoverable: "可解码时间仅代表扫描覆盖范围，不是质量或恢复率评分。",
    previews: "结果对比", original: "源视频预览", faithful: "保真抢救", improved: "改善观看", unsupported: "未发布改善结果。", verification: "验证状态", verificationPassed: "已通过", verificationNeedsReview: "需要复核", verificationFailed: "未通过",
    downloadFaithful: "下载保真抢救版本", downloadImproved: "下载改善观看版本", downloadReport: "下载技术报告", downloadJson: "下载 JSON", openHtml: "打开 HTML 报告",
    locked: "保护源视频区间", lockedStart: "保护区间开始时间（秒）", lockedEnd: "保护区间结束时间（秒）", addLocked: "添加保护区间", removeLocked: "移除保护区间", invalidLocked: "请输入有序且非负的保护区间。", maximumStrength: "平衡改善最大强度", advanced: "高级动作", processing: "正在本地处理和验证", completed: "已验证的抢救结果可用", partial: "部分抢救", needsReview: "需要复核", failed: "抢救失败", cancelled: "已取消抢救", action: "应用此动作", strength: "签发强度", strategyConservative: "保留内容，仅修复影响播放的结构问题。", strategyBalanced: "在私有预览确认后增加经测量、受限的 CPU 改善。", timelineAria: "可观察损坏与保护区间时间轴", coverage: "扫描到的可解码时间覆盖", noScore: "这是测量到的时间覆盖，不是恢复率或质量分数。",
  },
} as const;

export type RescueTextKey = keyof typeof text.en;
export function rescueText(key: RescueTextKey, locale: RescueLocale): string { return text[locale][key]; }

const symptomText: Record<RescueLocale, Record<import("./types").RescueSymptom, string>> = {
  en: { unplayable: "Cannot play", timeline_discontinuity: "Timeline jumps", missing_audio: "Missing audio", audio_video_offset: "Audio and video are out of sync", dark: "Picture is too dark", video_noise: "Visible video noise", soft_detail: "Soft detail", flicker: "Brightness flicker", shake: "Camera shake", low_loudness: "Audio is too quiet", audio_noise: "Audio noise", audio_clipping: "Audio clipping" },
  "zh-CN": { unplayable: "无法播放", timeline_discontinuity: "时间轴跳变", missing_audio: "缺少声音", audio_video_offset: "音画不同步", dark: "画面过暗", video_noise: "画面噪点", soft_detail: "细节偏软", flicker: "亮度闪烁", shake: "画面抖动", low_loudness: "声音过小", audio_noise: "音频噪声", audio_clipping: "音频削波" },
};

export function rescueSymptomText(symptom: import("./types").RescueSymptom, locale: RescueLocale): string { return symptomText[locale][symptom]; }

const statusText: Record<RescueLocale, Record<import("./types").RescueJobStatus, string>> = {
  en: { queued: "Queued", scanning: "Scanning", planning: "Planning", previewing: "Rendering previews", awaiting_confirmation: "Awaiting confirmation", processing: "Processing", verifying: "Verifying", completed: "Completed", needs_review: "Needs review", partial: "Partial", failed: "Failed", cancelled: "Cancelled" },
  "zh-CN": { queued: "排队中", scanning: "扫描中", planning: "规划中", previewing: "生成预览", awaiting_confirmation: "等待确认", processing: "处理中", verifying: "复检中", completed: "已完成", needs_review: "需要复核", partial: "部分完成", failed: "失败", cancelled: "已取消" },
};

export function rescueStatusText(status: import("./types").RescueJobStatus, locale: RescueLocale): string { return statusText[locale][status]; }
