import { rm } from "node:fs/promises";
import path from "node:path";

import { defineConfig } from "vite";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";

const buildOnlyPublicMetadata = [
  ["media", "media-sources.json"],
  ["media", "PROVENANCE.md"],
  ["cases", "PROVENANCE.md"],
];

function removeBuildOnlyPublicMetadata(): Plugin {
  let distributionDirectory: string | undefined;

  return {
    name: "videoscope-remove-build-only-public-metadata",
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
        buildOnlyPublicMetadata.map((segments) =>
          rm(path.join(resolvedDistributionDirectory, ...segments), {
            force: true,
          }),
        ),
      );
    },
  };
}

export default defineConfig({
  base: "/VideoScope/",
  plugins: [react(), removeBuildOnlyPublicMetadata()],
});
