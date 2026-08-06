import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const webDirectory = resolve(scriptDirectory, "..");
const source = resolve(webDirectory, "dist");
const destination = resolve(
  webDirectory,
  "..",
  "src",
  "videoscope",
  "web",
  "static",
);

await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await cp(source, destination, { recursive: true });
const indexPath = resolve(destination, "index.html");
const indexMarkup = await readFile(indexPath, "utf8");
await writeFile(indexPath, indexMarkup.replace(/\r\n?/g, "\n"), "utf8");
console.log(`Synced production dashboard to ${destination}`);
