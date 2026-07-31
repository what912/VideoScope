import type {
  BrowserAnalysisOptions,
  BrowserDetectorConfig,
} from "./contracts";

export interface NearBlackConfig extends BrowserDetectorConfig {
  enabled: boolean;
  mean_luma_threshold: number;
  dark_pixel_threshold: number;
  dark_pixel_ratio: number;
  min_duration_seconds: number;
  merge_gap_seconds: number;
  medium_severity_threshold: number;
  high_severity_threshold: number;
}

export interface PossibleFreezeConfig extends BrowserDetectorConfig {
  enabled: boolean;
  max_pixel_difference: number;
  max_hash_distance: number;
  min_duration_seconds: number;
  merge_gap_seconds: number;
  medium_severity_threshold: number;
  high_severity_threshold: number;
}

export interface SceneRelativeBlurConfig extends BrowserDetectorConfig {
  enabled: boolean;
  relative_ratio_threshold: number;
  absolute_floor: number;
  min_duration_seconds: number;
  merge_gap_seconds: number;
  medium_severity_threshold: number;
  high_severity_threshold: number;
}

export interface GlobalFlickerConfig extends BrowserDetectorConfig {
  enabled: boolean;
  residual_threshold: number;
  boundary_residual_ratio: number;
  minimum_cycles: number;
  min_duration_seconds: number;
  scene_boundary_guard_seconds: number;
  medium_severity_threshold: number;
  high_severity_threshold: number;
}

const defaultSeverityThresholds = {
  medium_severity_threshold: 0.65,
  high_severity_threshold: 0.9,
};

export const defaultNearBlackConfig: NearBlackConfig = {
  enabled: true,
  mean_luma_threshold: 14,
  dark_pixel_threshold: 16,
  dark_pixel_ratio: 0.94,
  min_duration_seconds: 1,
  merge_gap_seconds: 0.25,
  ...defaultSeverityThresholds,
};

export const defaultPossibleFreezeConfig: PossibleFreezeConfig = {
  enabled: true,
  max_pixel_difference: 1.5,
  max_hash_distance: 2,
  min_duration_seconds: 1.5,
  merge_gap_seconds: 0.25,
  ...defaultSeverityThresholds,
};

export const defaultSceneRelativeBlurConfig: SceneRelativeBlurConfig = {
  enabled: true,
  relative_ratio_threshold: 0.45,
  absolute_floor: 12,
  min_duration_seconds: 0.75,
  merge_gap_seconds: 0.25,
  ...defaultSeverityThresholds,
};

export const defaultGlobalFlickerConfig: GlobalFlickerConfig = {
  enabled: true,
  residual_threshold: 18,
  boundary_residual_ratio: 0.5,
  minimum_cycles: 2,
  min_duration_seconds: 0.75,
  scene_boundary_guard_seconds: 0.25,
  ...defaultSeverityThresholds,
};

export const defaultBrowserAnalysisOptions: BrowserAnalysisOptions = {
  sample_fps: 2,
  max_samples: 900,
  max_dimension: 320,
  evidence_max_dimension: 320,
  evidence_quality: 0.72,
  max_evidence_items: 24,
  max_evidence_total_bytes: 3 * 1024 * 1024,
  dark_pixel_threshold: defaultNearBlackConfig.dark_pixel_threshold,
  scene_cut_difference_threshold: 34,
  retain_prompt: false,
  locale: "en",
  reduced_motion: false,
  detectors: {
    near_black: defaultNearBlackConfig,
    possible_freeze: defaultPossibleFreezeConfig,
    scene_relative_blur: defaultSceneRelativeBlurConfig,
    global_flicker: defaultGlobalFlickerConfig,
  },
};

function requireFinite(
  value: unknown,
  name: string,
  minimum: number,
  maximum = Number.POSITIVE_INFINITY,
) {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new TypeError(`${name} must be between ${minimum} and ${maximum}`);
  }
}

function requireInteger(
  value: unknown,
  name: string,
  minimum: number,
  maximum: number,
) {
  requireFinite(value, name, minimum, maximum);
  if (!Number.isInteger(value)) {
    throw new TypeError(`${name} must be an integer`);
  }
}

function requireBoolean(value: unknown, name: string) {
  if (typeof value !== "boolean") {
    throw new TypeError(`${name} must be a boolean`);
  }
}

function validateSeverityThresholds(
  config: BrowserDetectorConfig,
  detectorId: string,
) {
  requireFinite(
    config.medium_severity_threshold,
    `${detectorId}.medium_severity_threshold`,
    0,
    1,
  );
  requireFinite(
    config.high_severity_threshold,
    `${detectorId}.high_severity_threshold`,
    0,
    1,
  );
  if (
    (config.medium_severity_threshold as number) >
    (config.high_severity_threshold as number)
  ) {
    throw new TypeError(
      `${detectorId} severity thresholds must be ordered`,
    );
  }
}

export function validateBrowserAnalysisOptions(
  options: BrowserAnalysisOptions,
): void {
  requireFinite(options.sample_fps, "sample_fps", 0.1, 12);
  requireInteger(options.max_samples, "max_samples", 1, 5_000);
  requireInteger(options.max_dimension, "max_dimension", 32, 1_280);
  requireInteger(
    options.evidence_max_dimension,
    "evidence_max_dimension",
    32,
    1_280,
  );
  requireFinite(options.evidence_quality, "evidence_quality", 0.1, 0.95);
  requireInteger(
    options.max_evidence_items,
    "max_evidence_items",
    1,
    100,
  );
  requireInteger(
    options.max_evidence_total_bytes,
    "max_evidence_total_bytes",
    1_024,
    20 * 1024 * 1024,
  );
  requireInteger(
    options.dark_pixel_threshold,
    "dark_pixel_threshold",
    0,
    255,
  );
  requireFinite(
    options.scene_cut_difference_threshold,
    "scene_cut_difference_threshold",
    0,
    255,
  );
  requireBoolean(options.retain_prompt, "retain_prompt");
  requireBoolean(options.reduced_motion, "reduced_motion");
  if (options.locale !== "en" && options.locale !== "zh-CN") {
    throw new TypeError("locale must be en or zh-CN");
  }
  Object.entries(options.detectors).forEach(([detectorId, config]) => {
    requireBoolean(config.enabled, `${detectorId}.enabled`);
  });
  const nearBlack = options.detectors.near_black;
  if (nearBlack) {
    validateSeverityThresholds(nearBlack, "near_black");
    requireFinite(
      nearBlack.mean_luma_threshold,
      "near_black.mean_luma_threshold",
      0,
      255,
    );
    requireInteger(
      nearBlack.dark_pixel_threshold,
      "near_black.dark_pixel_threshold",
      0,
      255,
    );
    requireFinite(
      nearBlack.dark_pixel_ratio,
      "near_black.dark_pixel_ratio",
      0,
      1,
    );
    requireFinite(
      nearBlack.min_duration_seconds,
      "near_black.min_duration_seconds",
      0,
    );
    requireFinite(
      nearBlack.merge_gap_seconds,
      "near_black.merge_gap_seconds",
      0,
    );
  }
  const freeze = options.detectors.possible_freeze;
  if (freeze) {
    validateSeverityThresholds(freeze, "possible_freeze");
    requireFinite(
      freeze.max_pixel_difference,
      "possible_freeze.max_pixel_difference",
      0,
      255,
    );
    requireInteger(
      freeze.max_hash_distance,
      "possible_freeze.max_hash_distance",
      0,
      64,
    );
    requireFinite(
      freeze.min_duration_seconds,
      "possible_freeze.min_duration_seconds",
      0,
    );
    requireFinite(
      freeze.merge_gap_seconds,
      "possible_freeze.merge_gap_seconds",
      0,
    );
  }
  const blur = options.detectors.scene_relative_blur;
  if (blur) {
    validateSeverityThresholds(blur, "scene_relative_blur");
    requireFinite(
      blur.relative_ratio_threshold,
      "scene_relative_blur.relative_ratio_threshold",
      0,
      1,
    );
    requireFinite(
      blur.absolute_floor,
      "scene_relative_blur.absolute_floor",
      0,
    );
    requireFinite(
      blur.min_duration_seconds,
      "scene_relative_blur.min_duration_seconds",
      0,
    );
    requireFinite(
      blur.merge_gap_seconds,
      "scene_relative_blur.merge_gap_seconds",
      0,
    );
  }
  const flicker = options.detectors.global_flicker;
  if (flicker) {
    validateSeverityThresholds(flicker, "global_flicker");
    requireFinite(
      flicker.residual_threshold,
      "global_flicker.residual_threshold",
      0,
      255,
    );
    requireFinite(
      flicker.boundary_residual_ratio,
      "global_flicker.boundary_residual_ratio",
      0,
      1,
    );
    requireInteger(
      flicker.minimum_cycles,
      "global_flicker.minimum_cycles",
      1,
      100,
    );
    requireFinite(
      flicker.min_duration_seconds,
      "global_flicker.min_duration_seconds",
      0,
    );
    requireFinite(
      flicker.scene_boundary_guard_seconds,
      "global_flicker.scene_boundary_guard_seconds",
      0,
    );
  }
}
