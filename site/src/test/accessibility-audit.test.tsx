import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { auditAccessibility } from "./accessibility-audit";

describe("accessibility audit", () => {
  afterEach(cleanup);

  it("reports structural, naming, focus, dialog, and non-color failures", () => {
    const view = render(
      <div>
        <h2>Out-of-order heading</h2>
        <button aria-controls="missing-panel" />
        <label>
          Unbound label
          <input />
        </label>
        <img src="local.webp" />
        <a href="/docs">
          Nested
          <button type="button">control</button>
        </a>
        <div role="button" tabIndex={-1}>
          Disabled custom control
        </div>
        <button tabIndex={2} type="button">
          Positive tab index
        </button>
        <section aria-label="Confirmation" role="dialog">
          Dialog content
        </section>
        <span data-severity="high" />
        <div id="duplicate" />
        <div id="duplicate" />
      </div>,
    );

    expect(auditAccessibility(view.container)).toEqual(
      expect.arrayContaining([
        "duplicate id #duplicate",
        "button has missing aria-controls #missing-panel",
        "page must contain exactly one h1",
        "first page heading must be h1",
        "button has no accessible name",
        "img has no alt attribute",
        "a nests an interactive control",
        "div has a non-focusable interactive role",
        "button uses positive tabindex",
        "section must declare aria-modal=true",
        "span exposes severity by color only",
      ]),
    );
  });

  it("accepts an explicitly hidden decorative severity signal", () => {
    const view = render(
      <main>
        <h1>Diagnostics</h1>
        <span aria-hidden="true" data-severity="high" />
        <p>High severity</p>
      </main>,
    );

    expect(auditAccessibility(view.container)).toEqual([]);
  });
});
