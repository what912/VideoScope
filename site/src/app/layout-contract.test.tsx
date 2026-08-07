import { readFileSync } from "node:fs";
import path from "node:path";

import { render, screen } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { TestApp } from "./router";

const globalsCss = readFileSync(
  path.resolve(process.cwd(), "src/styles/globals.css"),
  "utf8",
);
const tokensCss = readFileSync(
  path.resolve(process.cwd(), "src/styles/tokens.css"),
  "utf8",
);
const workspaceCss = readFileSync(
  path.resolve(process.cwd(), "src/features/workspace/workspace.css"),
  "utf8",
);

describe("application shell layout contract", () => {
  let styles: HTMLStyleElement;

  beforeAll(() => {
    styles = document.createElement("style");
    styles.textContent = `${tokensCss}\n${globalsCss}\n${workspaceCss}`;
    document.head.append(styles);
  });

  afterAll(() => styles.remove());

  it("uses a shrinkable single grid track so route content cannot widen the document", async () => {
    const view = render(<TestApp initialEntries={["/workspace"]} />);

    await screen.findByRole("heading", { level: 1 }, { timeout: 3_000 });
    const shellStyle = getComputedStyle(
      view.container.querySelector(".app-shell") as HTMLElement,
    );
    expect(shellStyle.gridTemplateColumns).toBe("minmax(0, 1fr)");
    expect(shellStyle.width).toBe("100%");
    expect(shellStyle.maxWidth).toBe("100%");

    const routeStyle = getComputedStyle(
      view.container.querySelector(".page-transition") as HTMLElement,
    );
    expect(routeStyle.minWidth).toBe("0");
    expect(routeStyle.maxWidth).toBe("100%");

    const toolbar = document.createElement("nav");
    toolbar.className = "workspace-toolbar";
    view.container.append(toolbar);
    const toolbarStyle = getComputedStyle(toolbar);
    expect(toolbarStyle.maxWidth).toBe("100%");
    expect(toolbarStyle.overflowX).toBe("auto");
  });
});
