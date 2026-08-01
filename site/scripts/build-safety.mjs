import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { URL } from "node:url";

export const documentedUrlExclusions = [
  {
    reason: "VideoScope project and creator navigation links",
    matches: (url) =>
      url.protocol === "https:" &&
      url.hostname === "github.com" &&
      url.pathname.startsWith("/what912"),
  },
  {
    reason: "React production diagnostic links embedded by React",
    matches: (url) =>
      url.protocol === "https:" &&
      url.hostname === "react.dev" &&
      url.pathname.startsWith("/errors/"),
  },
  {
    reason: "Supabase SDK diagnostic documentation",
    matches: (url) =>
      url.protocol === "https:" &&
      url.hostname === "github.com" &&
      url.pathname.startsWith("/orgs/supabase/discussions/"),
  },
  {
    reason: "Non-fetching direct URL input example",
    matches: (url) =>
      url.protocol === "https:" && url.hostname === "media.example",
  },
  {
    reason: "Inert W3C namespace identifiers embedded by React",
    matches: (url) =>
      url.protocol === "http:" &&
      url.hostname === "www.w3.org" &&
      /^\/(?:1998\/Math\/MathML|1999\/xlink|2000\/svg|XML\/1998\/namespace)$/u.test(
        url.pathname,
      ),
  },
  {
    reason: "Loopback-only defaults embedded by optional service SDKs",
    matches: (url) =>
      ["http:", "https:"].includes(url.protocol) &&
      ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname),
  },
  {
    reason: "Dependency diagnostic documentation links",
    matches: (url) =>
      url.protocol === "https:" &&
      ((url.hostname === "reactrouter.com" &&
        url.pathname.startsWith("/en/main/routers/")) ||
        (url.hostname === "github.com" &&
          url.pathname.startsWith("/ungap/url-search-params")) ||
        (url.hostname === "developer.mozilla.org" &&
          url.pathname.startsWith("/en-US/docs/Web/API/LockManager/"))),
  },
];

async function isFile(filePath) {
  try {
    return (await stat(filePath)).isFile();
  } catch {
    return false;
  }
}

async function buildFiles(distributionDirectory) {
  const indexPath = path.join(distributionDirectory, "index.html");
  if (!(await isFile(indexPath))) {
    throw new Error(
      `Production build is missing at ${indexPath}; run npm run build first.`,
    );
  }
  const assetsDirectory = path.join(distributionDirectory, "assets");
  let assetNames;
  try {
    assetNames = await readdir(assetsDirectory);
  } catch {
    throw new Error(
      `Production build assets are missing at ${assetsDirectory}; run npm run build first.`,
    );
  }
  return [
    indexPath,
    ...assetNames
      .filter((name) => /\.(?:css|js)$/u.test(name))
      .map((name) => path.join(assetsDirectory, name)),
  ];
}

function remoteUrls(contents) {
  return [
    ...contents.matchAll(
      /https?:\/\/[A-Za-z0-9.-]+(?::[0-9]+)?(?:\/[^\s"'`<>\\]*)?/giu,
    ),
  ].map((match) => match[0].replace(/[),.;}\]]+$/u, ""));
}

export async function auditBuiltRuntimeUrls(distributionDirectory) {
  const files = await buildFiles(distributionDirectory);
  const rejected = [];
  let urlCount = 0;

  for (const filePath of files) {
    const contents = await readFile(filePath, "utf8");
    for (const reference of remoteUrls(contents)) {
      urlCount += 1;
      let url;
      try {
        url = new URL(reference);
      } catch {
        rejected.push({ filePath, reference });
        continue;
      }
      if (!documentedUrlExclusions.some((entry) => entry.matches(url))) {
        rejected.push({ filePath, reference });
      }
    }
  }

  if (rejected.length > 0) {
    const details = rejected
      .slice(0, 10)
      .map(
        ({ filePath, reference }) =>
          `${path.relative(distributionDirectory, filePath)}: ${reference}`,
      )
      .join("\n");
    throw new Error(`Built output contains an unapproved remote URL:\n${details}`);
  }

  return { checkedFiles: files.length, urlCount };
}
