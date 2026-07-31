import { expect, it } from "vitest";

import {
  sceneDefinitions,
  stillFilterFor,
  videoFilterFor,
} from "./original-scenes.mjs";

const sceneCases = [
  { scene: "optical-aperture", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, durationSeconds: 8 },
  { scene: "night-observation-grid", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, durationSeconds: 6 },
  { scene: "fluid-spectrum", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, durationSeconds: 6 },
  { scene: "diagnostic-mesh", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, durationSeconds: 6 },
  { scene: "cool-topography", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, durationSeconds: 6 },
  { scene: "dawn-spectrum", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, durationSeconds: 6 },
  { scene: "cyan-caustic", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, stillTimeSeconds: 2 },
  { scene: "violet-lattice", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, stillTimeSeconds: 2 },
  { scene: "amber-contour", workingWidth: 1920, workingHeight: 1080, outputWidth: 1280, outputHeight: 720, frameRate: 24, stillTimeSeconds: 2 },
];

it("defines nine visually distinct project-authored scenes", () => {
  expect(Object.keys(sceneDefinitions)).toEqual([
    "optical-aperture", "night-observation-grid", "fluid-spectrum",
    "diagnostic-mesh", "cool-topography", "dawn-spectrum",
    "cyan-caustic", "violet-lattice", "amber-contour",
  ]);
  expect(new Set(Object.values(sceneDefinitions).map((scene) => scene.palette))).toHaveLength(9);
  expect(new Set(Object.values(sceneDefinitions).map((scene) => scene.signature))).toHaveLength(9);
});

it("produces local lavfi graphs without text, URL, or file input", () => {
  for (const item of sceneCases) {
    const filter = "durationSeconds" in item ? videoFilterFor(item) : stillFilterFor(item);
    expect(filter).toMatch(/^color=/u);
    expect(filter).not.toMatch(/https?:|movie=|drawtext=|subtitles=/iu);
    expect(filter).toContain("format=yuv420p");
  }
});

it("uses manifest dimensions and frame rate for every graph", () => {
  for (const item of sceneCases) {
    const filter = "durationSeconds" in item ? videoFilterFor(item) : stillFilterFor(item);
    expect(filter).toContain(`s=${item.workingWidth}x${item.workingHeight}`);
    expect(filter).toContain(`r=${item.frameRate}`);
    expect(filter).toContain(
      `scale=${item.outputWidth}:${item.outputHeight}:flags=lanczos,format=yuv420p`,
    );
  }
});

it("references time for motion in every video graph", () => {
  for (const item of sceneCases.filter((candidate) => "durationSeconds" in candidate)) {
    expect(videoFilterFor(item)).toMatch(/\bt\b/u);
  }
});

it("builds deterministic evidence still graphs", () => {
  for (const item of sceneCases.filter((candidate) => "stillTimeSeconds" in candidate)) {
    expect(stillFilterFor(item)).toBe(stillFilterFor({ ...item }));
  }
});
