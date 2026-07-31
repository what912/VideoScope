export type BrowserAnalysisLocale = "en" | "zh-CN";
export type BrowserDetectorId =
  | "near_black"
  | "possible_freeze"
  | "scene_relative_blur"
  | "global_flicker";

interface DetectorCopy {
  title: string;
  description: string;
  evidence: string;
  limitations: readonly string[];
}

interface BlurDetectorCopy extends DetectorCopy {
  description_relative: string;
  description_absolute: string;
  description_both: string;
}

interface AnalysisCopy {
  sampleWarning: string;
  sampleCapWarning: string;
  desktopWarning: string;
  evidenceCapWarning: string;
  evidenceCapLimitation: string;
  detectorMetricSuffix: string;
  detectorMetricDescription: string;
}

interface BrowserAnalysisCatalog {
  detectors: {
    near_black: DetectorCopy;
    possible_freeze: DetectorCopy;
    scene_relative_blur: BlurDetectorCopy;
    global_flicker: DetectorCopy;
  };
  analysis: AnalysisCopy;
}

const en: BrowserAnalysisCatalog = {
  detectors: {
    near_black: {
      title: "Near-black interval detected",
      description:
        "Sampled frames remain below the configured luminance threshold across this interval.",
      evidence:
        "Representative sampled frame from the near-black interval.",
      limitations: [
        "The interval may be an intentional black field, fade, or night scene.",
        "Browser sampling can miss brief changes between sampled frames.",
      ],
    },
    possible_freeze: {
      title: "Possible frozen or repeated frames",
      description:
        "Adjacent sampled frames remain visually similar by pixel and perceptual-hash differences.",
      evidence:
        "Beginning, middle, or end sample from the repeated-frame interval.",
      limitations: [
        "A deliberately static shot can resemble frozen or repeated frames.",
        "Motion between sampled timestamps may not be observed.",
      ],
    },
    scene_relative_blur: {
      title: "Relative sharpness drop",
      description:
        "Sampled sharpness differs from the configured scene-local screening baseline.",
      description_relative:
        "Sampled sharpness remains below the configured fraction of this scene's median baseline.",
      description_absolute:
        "Sampled sharpness remains below the configured absolute screening floor across this interval.",
      description_both:
        "This interval contains sampled frames below the scene-relative threshold and sampled frames below the configured absolute screening floor.",
      evidence:
        "Representative frame from the detected sharpness interval.",
      limitations: [
        "Intentional soft focus, motion blur, or depth of field can lower this metric.",
        "The baseline is local to one inferred scene and is not an absolute focus judgment.",
      ],
    },
    global_flicker: {
      title: "Potential global luminance flicker",
      description:
        "Scene-local global luminance residuals alternate rapidly after removing a short trend.",
      evidence:
        "Sample nearest the strongest high-frequency luminance residual.",
      limitations: [
        "Intentional strobing or rapid global lighting changes can resemble flicker.",
        "Scene-boundary guards and trend removal reduce but cannot eliminate false positives.",
      ],
    },
  },
  analysis: {
    sampleWarning:
      "Browser CPU analysis uses bounded sampled frames and cannot inspect every encoded frame.",
    sampleCapWarning:
      "The configured sample cap was reached; sampling density was reduced to stay within the browser memory budget.",
    desktopWarning:
      "Use the desktop CLI for complete container, codec, audio, and frame-level diagnostics.",
    evidenceCapWarning:
      "Evidence thumbnails were capped by the configured item or byte budget.",
    evidenceCapLimitation:
      "Some representative thumbnails were omitted because the evidence budget was reached.",
    detectorMetricSuffix: "review intervals",
    detectorMetricDescription:
      "Detector-local interval count; it is not a universal quality score.",
  },
};

const zhCN: BrowserAnalysisCatalog = {
  detectors: {
    near_black: {
      title: "检测到近黑区间",
      description: "该区间内的采样帧持续低于已配置的亮度筛查阈值。",
      evidence: "近黑区间中的代表性采样帧。",
      limitations: [
        "该区间也可能是有意使用的黑场、淡出或夜景。",
        "浏览器抽样可能漏掉采样帧之间的短暂变化。",
      ],
    },
    possible_freeze: {
      title: "可能存在冻结或重复帧",
      description:
        "相邻采样帧在像素差异和感知哈希差异上持续保持相似。",
      evidence: "重复帧区间开始、中间或结束位置的采样帧。",
      limitations: [
        "有意保持静止的镜头可能呈现相似信号。",
        "采样时间点之间的运动可能未被观察到。",
      ],
    },
    scene_relative_blur: {
      title: "相对清晰度下降",
      description: "采样清晰度偏离已配置的场景内筛查基线。",
      description_relative:
        "该区间的采样清晰度持续低于场景中位基线的已配置比例。",
      description_absolute:
        "该区间的采样清晰度持续低于已配置的绝对筛查下限。",
      description_both:
        "该区间包含低于场景相对阈值的采样帧，也包含低于已配置绝对筛查下限的采样帧。",
      evidence: "清晰度异常区间中的代表性采样帧。",
      limitations: [
        "有意柔焦、运动模糊或景深效果可能降低该指标。",
        "基线只属于当前推断场景，不代表绝对对焦结论。",
      ],
    },
    global_flicker: {
      title: "潜在的全局亮度闪烁",
      description:
        "移除短期趋势后，场景内全局亮度残差出现快速交替变化。",
      evidence: "最强高频亮度残差附近的采样帧。",
      limitations: [
        "有意频闪或快速全局灯光变化可能呈现相似信号。",
        "切镜保护和趋势移除可以减少但不能消除误报。",
      ],
    },
  },
  analysis: {
    sampleCapWarning:
      "已达到配置的抽样上限；为控制浏览器内存占用，系统降低了抽样密度。",
    sampleWarning:
      "浏览器 CPU 分析使用有界采样帧，无法检查每一个编码帧。",
    desktopWarning:
      "如需完整的容器、编解码器、音频和逐帧诊断，请使用桌面 CLI。",
    evidenceCapWarning:
      "证据缩略图已达到配置的数量或字节预算上限。",
    evidenceCapLimitation:
      "由于达到证据预算上限，部分代表性缩略图未被保留。",
    detectorMetricSuffix: "个复核区间",
    detectorMetricDescription:
      "这是检测器内部的区间数量，不是通用质量分数。",
  },
};

const catalogs: Record<BrowserAnalysisLocale, BrowserAnalysisCatalog> = {
  en,
  "zh-CN": zhCN,
};

export function getDetectorCopy<DetectorId extends BrowserDetectorId>(
  locale: BrowserAnalysisLocale,
  detectorId: DetectorId,
): BrowserAnalysisCatalog["detectors"][DetectorId] {
  return catalogs[locale].detectors[detectorId];
}

export function getAnalysisCopy(locale: BrowserAnalysisLocale): AnalysisCopy {
  return catalogs[locale].analysis;
}
