import type { BrowserSample, BrowserScene } from "./contracts";

export function segmentScenes(
  samples: readonly BrowserSample[],
  durationSeconds: number,
  differenceThreshold: number,
): BrowserScene[] {
  if (durationSeconds <= 0) {
    return [];
  }
  if (samples.length === 0) {
    return [
      {
        scene_index: 0,
        start_seconds: 0,
        end_seconds: durationSeconds,
        representative_timestamp: durationSeconds / 2,
      },
    ];
  }
  const boundaries = [0];
  for (let index = 1; index < samples.length; index += 1) {
    const previousIsStable =
      index === 1 ||
      samples[index - 1].pixel_difference < differenceThreshold;
    const nextIsStable =
      index === samples.length - 1 ||
      samples[index + 1].pixel_difference < differenceThreshold;
    if (
      samples[index].pixel_difference >= differenceThreshold &&
      previousIsStable &&
      nextIsStable
    ) {
      boundaries.push(
        (samples[index - 1].timestamp_seconds +
          samples[index].timestamp_seconds) /
          2,
      );
    }
  }
  boundaries.push(durationSeconds);
  return boundaries.slice(0, -1).map((start, index) => {
    const end = boundaries[index + 1];
    return {
      scene_index: index,
      start_seconds: start,
      end_seconds: end,
      representative_timestamp: (start + end) / 2,
    };
  });
}

export function samplesInScene(
  samples: readonly BrowserSample[],
  scene: BrowserScene,
): BrowserSample[] {
  return samples.filter(
    (sample) =>
      sample.timestamp_seconds >= scene.start_seconds &&
      sample.timestamp_seconds < scene.end_seconds,
  );
}
