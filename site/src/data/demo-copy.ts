import type { Locale } from "../i18n/types";

export type DemoFindingId =
  | "demo-flicker"
  | "demo-hand-geometry"
  | "demo-background-warping"
  | "demo-text-instability"
  | "demo-motion-jitter";

export type DemoMetricId =
  | "luminance-stability"
  | "relative-sharpness"
  | "frame-change"
  | "dark-interval-screening"
  | "geometry-consistency"
  | "text-stability"
  | "background-stability"
  | "motion-continuity";

interface FindingCopy {
  readonly title: string;
  readonly description: string;
  readonly limitations: readonly string[];
}

interface MetricCopy {
  readonly label: string;
  readonly description: string;
}

export interface DemoCopy {
  readonly reportTitle: string;
  readonly evidenceFrame: string;
  readonly warnings: readonly [string, string];
  readonly findings: Record<DemoFindingId, FindingCopy>;
  readonly metrics: Record<DemoMetricId, MetricCopy>;
}

export const demoCopyByLocale: Record<Locale, DemoCopy> = {
  en: {
    reportTitle: "Video Observatory interactive demonstration",
    evidenceFrame: "demonstration frame",
    warnings: [
      "INTERACTIVE DEMO: intervals and values are curated product examples, not measurements from the stock video.",
      "Browser analysis has limited access to codec, timestamp, audio, and container metadata; use the desktop CLI for complete analysis.",
    ],
    findings: {
      "demo-flicker": {
        title: "Temporal Flicker",
        description:
          "A browser CPU luminance signal changes rapidly across this interval.",
        limitations: [
          "Rapid lighting changes and intentional strobing can resemble flicker.",
          "This interactive fixture is not a measured result from the stock video.",
        ],
      },
      "demo-hand-geometry": {
        title: "Hand Geometry Distortion",
        description:
          "An optional visual-consistency demo signal marks an abrupt shape change.",
        limitations: [
          "This optional demo signal is not produced by the browser CPU detector set.",
          "Occlusion, motion blur, and perspective can cause similar visual changes.",
        ],
      },
      "demo-background-warping": {
        title: "Background Warping",
        description:
          "An optional visual-consistency demo signal marks background structure change.",
        limitations: [
          "This optional demo signal is not produced by the browser CPU detector set.",
          "Camera movement and parallax can resemble background instability.",
        ],
      },
      "demo-text-instability": {
        title: "Text Instability",
        description:
          "An optional OCR demo signal marks changing glyph appearance in one region.",
        limitations: [
          "This optional demo signal requires OCR and is not part of browser CPU analysis.",
          "Recognition errors can create false positives.",
        ],
      },
      "demo-motion-jitter": {
        title: "Motion Jitter",
        description:
          "An optional motion demo signal marks uneven apparent displacement.",
        limitations: [
          "This optional demo signal is not produced by the browser CPU detector set.",
          "Handheld camera motion can resemble motion jitter.",
        ],
      },
    },
    metrics: {
      "luminance-stability": {
        label: "Luminance stability",
        description: "Detector-local stability signal; higher is steadier.",
      },
      "relative-sharpness": {
        label: "Relative sharpness stability",
        description: "Scene-relative demo value, not a cross-detector score.",
      },
      "frame-change": {
        label: "Frame-change continuity",
        description:
          "Detector-local demo value for repeated-frame screening.",
      },
      "dark-interval-screening": {
        label: "Dark-interval screening",
        description:
          "Detector-local demo value for near-black interval screening.",
      },
      "geometry-consistency": {
        label: "Geometry consistency (DEMO)",
        description:
          "Optional demonstration signal; not a browser CPU result.",
      },
      "text-stability": {
        label: "Text stability (DEMO)",
        description: "Optional OCR demonstration signal.",
      },
      "background-stability": {
        label: "Background stability (DEMO)",
        description: "Optional visual-consistency demonstration signal.",
      },
      "motion-continuity": {
        label: "Motion continuity (DEMO)",
        description: "Optional motion demonstration signal.",
      },
    },
  },
  "zh-CN": {
    reportTitle: "视频观测站交互演示",
    evidenceFrame: "演示证据帧",
    warnings: [
      "交互演示：区间和数值为产品演示数据，并非对背景视频的实测结果。",
      "浏览器分析无法完整读取编解码器、时间戳、音频和容器元数据；完整分析请使用桌面 CLI。",
    ],
    findings: {
      "demo-flicker": {
        title: "时间闪烁",
        description: "浏览器 CPU 亮度信号在这个区间内快速变化。",
        limitations: [
          "快速光照变化和刻意频闪可能呈现相似信号。",
          "此交互样例并非对背景视频的实测结果。",
        ],
      },
      "demo-hand-geometry": {
        title: "手部几何形变",
        description: "可选视觉一致性演示信号标记了突然的形状变化。",
        limitations: [
          "此可选演示信号并非由浏览器 CPU 检测器生成。",
          "遮挡、运动模糊和透视变化可能产生相似现象。",
        ],
      },
      "demo-background-warping": {
        title: "背景扭曲",
        description: "可选视觉一致性演示信号标记了背景结构变化。",
        limitations: [
          "此可选演示信号并非由浏览器 CPU 检测器生成。",
          "镜头运动和视差可能看起来像背景不稳定。",
        ],
      },
      "demo-text-instability": {
        title: "文字不稳定",
        description: "可选 OCR 演示信号标记了同一区域内的字形变化。",
        limitations: [
          "此可选演示信号需要 OCR，不属于浏览器 CPU 分析。",
          "OCR 识别错误可能造成误报。",
        ],
      },
      "demo-motion-jitter": {
        title: "运动抖动",
        description: "可选运动演示信号标记了不均匀的表观位移。",
        limitations: [
          "此可选演示信号并非由浏览器 CPU 检测器生成。",
          "手持拍摄的镜头运动可能呈现相似抖动。",
        ],
      },
    },
    metrics: {
      "luminance-stability": {
        label: "亮度稳定性",
        description: "检测器内稳定信号；数值越高表示越稳定。",
      },
      "relative-sharpness": {
        label: "相对清晰度稳定性",
        description: "场景相对演示值，不是跨检测器总分。",
      },
      "frame-change": {
        label: "帧变化连续性",
        description: "用于重复帧筛查的检测器内演示值。",
      },
      "dark-interval-screening": {
        label: "暗画面区间筛查",
        description: "用于近黑区间筛查的检测器内演示值。",
      },
      "geometry-consistency": {
        label: "几何一致性（演示）",
        description: "可选演示信号，不是浏览器 CPU 结果。",
      },
      "text-stability": {
        label: "文字稳定性（演示）",
        description: "可选 OCR 演示信号。",
      },
      "background-stability": {
        label: "背景稳定性（演示）",
        description: "可选视觉一致性演示信号。",
      },
      "motion-continuity": {
        label: "运动连续性（演示）",
        description: "可选运动演示信号。",
      },
    },
  },
};
