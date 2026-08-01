import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

const siteRoot = process.cwd();

function contentSecurityPolicy(html: string): string {
  const document = new DOMParser().parseFromString(html, "text/html");
  return (
    document
      .querySelector('meta[http-equiv="Content-Security-Policy"]')
      ?.getAttribute("content") ?? ""
  );
}

describe("static deployment security", () => {
  it.each(["index.html", "public/404.html"])(
    "ships a restrictive CSP in %s",
    async (relativePath) => {
      const html = await readFile(path.join(siteRoot, relativePath), "utf8");
      const policy = contentSecurityPolicy(html);

      expect(policy).toContain("default-src 'none'");
      expect(policy).toContain("script-src 'self' 'wasm-unsafe-eval'");
      expect(policy).toContain("style-src 'self'");
      expect(policy).toContain("style-src-attr 'unsafe-inline'");
      expect(policy).toContain("img-src 'self' data: blob:");
      expect(policy).toContain("media-src 'self' blob:");
      expect(policy).toContain("connect-src 'self' https:");
      expect(policy).toContain("object-src 'none'");
      expect(policy).toContain("base-uri 'self'");
      expect(policy).toContain("form-action 'self'");
      expect(policy).not.toContain("'unsafe-eval'");
      expect(policy).not.toMatch(/script-src[^;]*'unsafe-inline'/u);
    },
  );

  it("authorizes the 404 bootstrap with a script hash", async () => {
    const html = await readFile(
      path.join(siteRoot, "public", "404.html"),
      "utf8",
    );
    const document = new DOMParser().parseFromString(html, "text/html");
    const bootstrap = document.querySelector("script")?.textContent ?? "";
    const expectedHash = createHash("sha256")
      .update(bootstrap)
      .digest("base64");

    expect(contentSecurityPolicy(html)).toContain(
      `'sha256-${expectedHash}'`,
    );
  });

  it("passes optional public Supabase settings into the Pages build", async () => {
    const workflow = await readFile(
      path.resolve(siteRoot, "..", ".github", "workflows", "pages.yml"),
      "utf8",
    );

    expect(workflow).toContain(
      "VITE_SUPABASE_URL: ${{ vars.VITE_SUPABASE_URL }}",
    );
    expect(workflow).toContain(
      "VITE_SUPABASE_ANON_KEY: ${{ secrets.VITE_SUPABASE_ANON_KEY }}",
    );
    expect(workflow).toContain(
      "VITE_SUPABASE_SHARE_ENABLED: ${{ vars.VITE_SUPABASE_SHARE_ENABLED }}",
    );
    expect(workflow).not.toMatch(/SERVICE_ROLE/iu);
  });
});
