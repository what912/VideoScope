import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { describe, expect, it } from "vitest";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);

async function readRepositoryFile(relativePath) {
  return readFile(path.join(repositoryRoot, relativePath), "utf8");
}

describe("public release documentation", () => {
  it.each([
    "README.md",
    "docs/public-site.md",
    "docs/public-site-release-checklist.md",
  ])("records the public site as live in %s", async (relativePath) => {
    const content = await readRepositoryFile(relativePath);

    expect(content).toContain("https://github.com/what912/VideoScope");
    expect(content).toContain("https://what912.github.io/VideoScope/");
    expect(content).not.toMatch(/NOT DEPLOYED|candidate public URL|not deployed/i);
  });

  it("describes the actual React and Vite site without starter auth claims", async () => {
    const content = await readRepositoryFile("site/README.md");

    expect(content).toMatch(/React/i);
    expect(content).toMatch(/TypeScript/i);
    expect(content).toMatch(/Vite/i);
    expect(content).toContain("/VideoScope/");
    expect(content).not.toMatch(/vinext|ChatGPT|Dispatch|D1|Drizzle/i);
  });

  it("labels immutable deployment evidence as the initial public baseline", async () => {
    const publicSite = await readRepositoryFile("docs/public-site.md");
    const releaseChecklist = await readRepositoryFile(
      "docs/public-site-release-checklist.md",
    );

    expect(publicSite).toMatch(/initial public deployment baseline/i);
    expect(releaseChecklist).toMatch(/initial public deployment baseline/i);
    expect(publicSite).not.toMatch(/published source is the reviewed root commit/i);
  });

  it("inventories every direct site dependency with its resolved version and license", async () => {
    const packageJson = JSON.parse(await readRepositoryFile("site/package.json"));
    const packageLock = JSON.parse(
      await readRepositoryFile("site/package-lock.json"),
    );
    const inventory = await readRepositoryFile("docs/third-party-licenses.md");

    for (const dependencyName of Object.keys({
      ...packageJson.dependencies,
      ...packageJson.devDependencies,
    })) {
      const metadata = packageLock.packages[`node_modules/${dependencyName}`];
      expect(metadata, `${dependencyName} lock metadata`).toBeDefined();
      expect(inventory, `${dependencyName} inventory entry`).toContain(
        `\`${dependencyName}\``,
      );
      expect(inventory, `${dependencyName} resolved version`).toContain(
        `\`${metadata.version}\``,
      );
      expect(inventory, `${dependencyName} license`).toContain(
        `\`${metadata.license}\``,
      );
    }
  });

  it("configures Dependabot for both Node workspaces", async () => {
    const configuration = await readRepositoryFile(".github/dependabot.yml");

    expect(configuration).toMatch(
      /package-ecosystem:\s*npm[\s\S]*?directory:\s*\/web/u,
    );
    expect(configuration).toMatch(
      /package-ecosystem:\s*npm[\s\S]*?directory:\s*\/site/u,
    );
  });
});
