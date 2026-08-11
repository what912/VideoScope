import { execFile } from "node:child_process";
import { access, mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import { auditBuiltRuntimeUrls } from "./build-safety.mjs";

const temporaryDirectories = [];
const execFileAsync = promisify(execFile);
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const siteDirectory = path.resolve(scriptDirectory, "..");
const viteBinary = path.join(siteDirectory, "node_modules", "vite", "bin", "vite.js");

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

  it("excludes case provenance from the built public site", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "videoscope-case-build-"));
    temporaryDirectories.push(directory);

    await execFileAsync(process.execPath, [viteBinary, "build", "--outDir", directory], {
      cwd: siteDirectory,
      windowsHide: true,
    });

    await expect(access(path.join(directory, "cases", "PROVENANCE.md"))).rejects
      .toMatchObject({ code: "ENOENT" });
    await expect(access(path.join(directory, "cases", "timeline-rescue", "before.mp4")))
      .resolves.toBeUndefined();
  });
});
