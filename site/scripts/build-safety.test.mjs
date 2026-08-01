import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { auditBuiltRuntimeUrls } from "./build-safety.mjs";

const temporaryDirectories = [];

async function temporaryBuild() {
  const directory = await mkdtemp(path.join(os.tmpdir(), "videoscope-build-"));
  temporaryDirectories.push(directory);
  await mkdir(path.join(directory, "assets"));
  await writeFile(
    path.join(directory, "index.html"),
    '<script src="/VideoScope/assets/app.js"></script>',
  );
  return directory;
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { force: true, recursive: true }),
    ),
  );
});

describe("built runtime URL audit", () => {
  it("fails when a production build is missing", async () => {
    const missing = path.join(os.tmpdir(), "videoscope-missing-dist");

    await expect(auditBuiltRuntimeUrls(missing)).rejects.toThrow(
      /production build is missing/i,
    );
  });

  it("allows documented project and dependency reference URLs", async () => {
    const directory = await temporaryBuild();
    await writeFile(
      path.join(directory, "assets", "app.js"),
      [
        '"https://github.com/what912/VideoScope"',
        '"https://react.dev/errors/418"',
        '"https://github.com/orgs/supabase/discussions/123"',
        '"http://www.w3.org/2000/svg"',
        '"http://www.w3.org/XML/1998/namespace"',
        '"http://localhost:9999"',
        '"https://reactrouter.com/en/main/routers/picking-a-router"',
        '"https://github.com/ungap/url-search-params"',
        '"https://developer.mozilla.org/en-US/docs/Web/API/LockManager/request"',
      ].join(";"),
    );

    await expect(auditBuiltRuntimeUrls(directory)).resolves.toEqual(
      expect.objectContaining({ checkedFiles: 2 }),
    );
  });

  it("rejects a remote runtime asset left in a built chunk", async () => {
    const directory = await temporaryBuild();
    await writeFile(
      path.join(directory, "assets", "app.js"),
      'const poster = "https://cdn.example/diagnostic-poster.webp";',
    );

    await expect(auditBuiltRuntimeUrls(directory)).rejects.toThrow(
      /cdn\.example\/diagnostic-poster\.webp/u,
    );
  });
});
