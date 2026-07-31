import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

const sourceRoot = path.resolve(process.cwd(), "src");
const publicRoot = path.resolve(process.cwd(), "public");
const indexPath = path.resolve(process.cwd(), "index.html");

async function filesUnder(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  return (
    await Promise.all(
      entries.map(async (entry) => {
        const entryPath = path.join(directory, entry.name);
        return entry.isDirectory() ? filesUnder(entryPath) : [entryPath];
      }),
    )
  ).flat();
}

function isProductionSource(filePath: string) {
  return (
    /\.(?:css|ts|tsx)$/u.test(filePath) &&
    !/\.test\.(?:ts|tsx)$/u.test(filePath)
  );
}

function runtimeAssetReferences(contents: string) {
  const references: string[] = [];
  const patterns = [
    /(?:src|poster)\s*=\s*["'`]([^"'`]+)["'`]/giu,
    /url\(\s*["']?([^"')]+)["']?\s*\)/giu,
  ];
  for (const pattern of patterns) {
    for (const match of contents.matchAll(pattern)) {
      if (match[1]) references.push(match[1]);
    }
  }
  return references;
}

function anchorReferences(contents: string) {
  return [...contents.matchAll(/href\s*=\s*["'`]([^"'`]+)["'`]/giu)]
    .map((match) => match[1])
    .filter((reference): reference is string => reference !== undefined);
}

describe("offline runtime asset policy", () => {
  it("uses no forbidden remote font, stock-media, or insecure runtime asset", async () => {
    const files = [
      indexPath,
      ...(await filesUnder(sourceRoot)).filter(isProductionSource),
      ...(await filesUnder(publicRoot)).filter((filePath) =>
        /\.(?:css|html|json|svg|webmanifest)$/u.test(filePath),
      ),
    ];
    const sources = await Promise.all(
      files.map(async (filePath) => readFile(filePath, "utf8")),
    );
    const references = (
      sources.map((contents) => runtimeAssetReferences(contents))
    ).flat();

    for (const reference of references) {
      expect(reference).not.toMatch(/^https?:\/\//iu);
    }

    const anchors = (
      await Promise.all(
        sources.map(async (contents) => anchorReferences(contents)),
      )
    ).flat();
    for (const anchor of anchors) {
      if (/^https?:\/\//iu.test(anchor)) {
        expect(anchor).toMatch(/^https:\/\/github\.com\/what912(?:\/|$)/iu);
      }
    }
  });

  it("does not opt product code into raw HTML or render error stacks", async () => {
    const sourceFiles = (await filesUnder(sourceRoot)).filter(
      isProductionSource,
    );
    const sources = await Promise.all(
      sourceFiles.map(async (filePath) => ({
        filePath,
        contents: await readFile(filePath, "utf8"),
      })),
    );

    for (const source of sources) {
      expect(source.contents, source.filePath).not.toContain(
        "dangerouslySetInnerHTML",
      );
      expect(source.contents, source.filePath).not.toMatch(
        /(?:error|exception|reason)\s*\.\s*stack\b/iu,
      );
    }
  });
});
