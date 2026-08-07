import { describe, expect, it } from "vitest";

import { normalizeStaticIndexMarkup } from "./static-normalization.mjs";

describe("normalizeStaticIndexMarkup", () => {
  it("produces identical packaged markup from LF and CRLF Vite inputs", () => {
    const lf = "<html>\n  <body>\n\n    <div id=\"root\"></div>\n  </body>\n</html>\n";
    const crlf = lf.replace(/\n/g, "\r\n");

    expect(normalizeStaticIndexMarkup(lf)).toBe(
      normalizeStaticIndexMarkup(crlf),
    );
    expect(normalizeStaticIndexMarkup(lf)).toBe(
      "<html>\n  <body>\n    <div id=\"root\"></div>\n  </body>\n</html>\n",
    );
  });
});
