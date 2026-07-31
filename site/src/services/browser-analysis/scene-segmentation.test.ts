import { describe, expect, it } from "vitest";

import type { BrowserSample } from "./contracts";
import { samplesInScene, segmentScenes } from "./scene-segmentation";

const sample = (
  timestamp: number,
  pixelDifference: number,
): BrowserSample => ({
  sample_index: timestamp,
  timestamp_seconds: timestamp,
  width: 8,
  height: 8,
  mean_luma: 100,
  median_luma: 100,
  dark_pixel_ratio: 0,
  sharpness: 10,
  pixel_difference: pixelDifference,
  hash_distance: 4,
});

describe("scene segmentation context", () => {
  it("creates continuous scene context without emitting findings", () => {
    const scenes = segmentScenes(
      [sample(0, 255), sample(1, 4), sample(2, 80), sample(3, 4)],
      4,
      40,
    );

    expect(scenes).toEqual([
      {
        scene_index: 0,
        start_seconds: 0,
        end_seconds: 1.5,
        representative_timestamp: 0.75,
      },
      {
        scene_index: 1,
        start_seconds: 1.5,
        end_seconds: 4,
        representative_timestamp: 2.75,
      },
    ]);
  });

  it("does not split a scene for a global luminance jump without structural change", () => {
    const scenes = segmentScenes(
      [sample(0, 255), sample(1, 90), sample(2, 90), sample(3, 4)],
      4,
      40,
    );

    expect(scenes).toEqual([
      {
        scene_index: 0,
        start_seconds: 0,
        end_seconds: 4,
        representative_timestamp: 2,
      },
    ]);
  });

  it("uses half-open scene intervals so a boundary sample belongs once", () => {
    const boundarySample = sample(2, 4);
    const all = [sample(1, 4), boundarySample, sample(3, 4)];

    expect(
      samplesInScene(all, {
        scene_index: 0,
        start_seconds: 0,
        end_seconds: 2,
        representative_timestamp: 1,
      }),
    ).toEqual([all[0]]);
    expect(
      samplesInScene(all, {
        scene_index: 1,
        start_seconds: 2,
        end_seconds: 4,
        representative_timestamp: 3,
      }),
    ).toEqual([boundarySample, all[2]]);
  });
});
