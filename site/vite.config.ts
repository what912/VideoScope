import { rm } from "node:fs/promises";
import path from "node:path";

import { defineConfig } from "vite";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";

const buildOnlyMediaMetadata = ["media-sources.json", "PROVENANCE.md"];

function removeBuildOnlyMediaMetadata(): Plugin {
  let distributionDirectory: string | undefined;

  return {
    name: "videoscope-remove-build-only-media-metadata",
    apply: "build",
    configResolved(config) {
      distributionDirectory = path.resolve(config.root, config.build.outDir);
    },
    async closeBundle() {
      if (distributionDirectory === undefined) {
        throw new Error("Vite distribution directory was not resolved");
      }
      const resolvedDistributionDirectory = distributionDirectory;
      await Promise.all(
        buildOnlyMediaMetadata.map((filename) =>
          rm(path.join(resolvedDistributionDirectory, "media", filename), {
            force: true,
          }),
        ),
      );
    },
  };
}

export default defineConfig({
  base: "/VideoScope/",
  plugins: [react(), removeBuildOnlyMediaMetadata()],
});
