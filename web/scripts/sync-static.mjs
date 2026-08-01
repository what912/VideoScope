import { cp, mkdir, rm } from "node:fs/promises";
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
console.log(`Synced production dashboard to ${destination}`);
