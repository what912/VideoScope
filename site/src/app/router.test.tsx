import { fireEvent, render, screen, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import viteConfig from "../../vite.config";
import { Header } from "../components/layout/Header";
import { AppProviders } from "./AppProviders";
import { TestApp } from "./router";

function renderWithShellProviders(element: ReactElement) {
  return render(
    <AppProviders>
      <MemoryRouter>{element}</MemoryRouter>
    </AppProviders>,
  );
}

describe("product routes", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it.each([
    ["/", "See what your video hides"],
    ["/workspace", "Workspace"],
    ["/compare", "Compare videos"],
  ])("renders %s", async (path, expected) => {
    render(<TestApp initialEntries={[path]} />);
    expect(await screen.findByText(expected, { exact: false })).toBeVisible();
    expect(screen.getByText("what912")).toBeVisible();
  });

  it("renders the explicit demo report route", async () => {
    render(<TestApp initialEntries={["/report/demo"]} />);

    expect(
      await screen.findByRole("heading", {
        name: "Video Observatory interactive demonstration",
      }),
    ).toBeVisible();
    expect(screen.getByText("what912")).toBeVisible();
  });
});

describe("application shell", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("exposes the observatory navigation and fixed creator mark", async () => {
    render(<TestApp initialEntries={["/"]} />);

    const navigation = await screen.findByRole("navigation", {
      name: "Primary navigation",
    });

    for (const label of [
      "Product",
      "Features",
      "Compare",
      "Research",
      "Open Source",
      "Docs",
      "GitHub",
    ]) {
      expect(within(navigation).getByRole("link", { name: label })).toBeVisible();
    }
    expect(
      within(navigation).getByRole("link", { name: "Research" }),
    ).toHaveAttribute(
      "href",
      "https://github.com/what912/VideoScope/tree/main/docs",
    );
    const header = screen.getByRole("banner");
    expect(
      within(header).getByRole("link", { name: "Analyze a video" }),
    ).toBeVisible();
    expect(screen.getByText("what912")).toBeVisible();
  });

  it("switches all shell copy to Simplified Chinese without navigation", async () => {
    render(<TestApp initialEntries={["/"]} />);

    const language = await screen.findByRole("combobox", { name: "Language" });
    fireEvent.change(language, { target: { value: "zh-CN" } });

    expect(
      screen.getByRole("navigation", { name: "主导航" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "产品" })).toBeVisible();
    expect(
      within(screen.getByRole("banner")).getByRole("link", {
        name: "分析视频",
      }),
    ).toBeVisible();
    expect(screen.getByText("what912")).toBeVisible();
  });

  it("traps mobile-menu focus and restores it after Escape", async () => {
    render(<TestApp initialEntries={["/"]} />);

    const trigger = await screen.findByRole("button", {
      name: "Open navigation menu",
    });
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Mobile navigation" });
    const focusable = within(dialog).getAllByRole("button").concat(
      within(dialog).getAllByRole("link"),
      within(dialog).getAllByRole("combobox"),
    );
    const first = focusable[0];
    const last = focusable.at(-1);

    expect(first).toHaveFocus();
    expect(last).toBeDefined();

    last?.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(first).toHaveFocus();

    first.focus();
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(
      screen.queryByRole("dialog", { name: "Mobile navigation" }),
    ).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("portals the mobile dialog outside the filtered header", async () => {
    render(<TestApp initialEntries={["/"]} />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Open navigation menu" }),
    );

    const header = screen.getByRole("banner");
    const dialog = screen.getByRole("dialog", { name: "Mobile navigation" });
    expect(header).not.toContainElement(dialog);
    expect(document.body).toContainElement(dialog);
  });

  it.each([
    ["Escape", "escape"],
    ["close button", "close"],
    ["internal navigation link", "Product"],
    ["optional Sign in action", "Sign in"],
    ["Analyze a video action", "Analyze a video"],
  ])("restores trigger focus after closing with %s", async (_, closePath) => {
    renderWithShellProviders(<Header showSignIn />);

    const trigger = screen.getByRole("button", {
      name: "Open navigation menu",
    });
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Mobile navigation" });
    if (closePath === "escape") {
      fireEvent.keyDown(dialog, { key: "Escape" });
    } else if (closePath === "close") {
      fireEvent.click(
        within(dialog).getByRole("button", {
          name: "Close navigation menu",
        }),
      );
    } else {
      fireEvent.click(
        within(dialog).getByRole("link", {
          name: closePath,
        }),
      );
    }

    expect(
      screen.queryByRole("dialog", { name: "Mobile navigation" }),
    ).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("provides a non-interactive print-only creator attribution", async () => {
    render(<TestApp initialEntries={["/"]} />);

    const attribution = await screen.findByTestId("print-attribution");
    expect(attribution).toHaveAttribute("aria-hidden", "true");
    expect(attribution).toHaveAttribute("data-attribution", "what912");
    expect(attribution).not.toHaveAttribute("href");
  });
});

const pagesRouteStorageKey = "videoscope:pages-route";
const pages404Path = resolve(process.cwd(), "public", "404.html");
const pagesWorkflowPath = resolve(
  process.cwd(),
  "..",
  ".github",
  "workflows",
  "pages.yml",
);

function executePages404(url: URL) {
  const stored = new Map<string, string>();
  const replace = vi.fn();
  const html = readFileSync(pages404Path, "utf8");
  const script = html.match(/<script>([\s\S]*?)<\/script>/u)?.[1];

  expect(script).toBeDefined();

  const redirectWindow = {
    location: {
      hash: url.hash,
      origin: url.origin,
      pathname: url.pathname,
      replace,
      search: url.search,
    },
    sessionStorage: {
      getItem(key: string) {
        return stored.get(key) ?? null;
      },
      removeItem(key: string) {
        stored.delete(key);
      },
      setItem(key: string, value: string) {
        stored.set(key, value);
      },
    },
  };

  new Function("window", script ?? "")(redirectWindow);

  return { replace, stored };
}

async function importMainWithoutMounting() {
  vi.resetModules();
  return import("../main");
}

describe("GitHub Pages routing", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/VideoScope/");
  });

  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/VideoScope/");
  });

  it("uses the project base for generated application assets", () => {
    expect(viteConfig).toMatchObject({ base: "/VideoScope/" });
  });

  it("generates and verifies original media before uploading the Pages artifact", () => {
    const workflow = readFileSync(pagesWorkflowPath, "utf8");
    const setupFfmpeg = workflow.indexOf("Install FFmpeg");
    const npmCi = workflow.indexOf("run: npm ci");
    const prepareMedia = workflow.indexOf("run: npm run media:prepare");
    const lint = workflow.indexOf("run: npm run lint");
    const typecheck = workflow.indexOf("run: npm run typecheck");
    const test = workflow.indexOf("run: npm test");
    const build = workflow.indexOf("run: npm run build");
    const verify = workflow.indexOf("run: npm run media:verify");
    const upload = workflow.indexOf("uses: actions/upload-pages-artifact@v3");

    expect(setupFfmpeg).toBeGreaterThan(-1);
    expect(npmCi).toBeGreaterThan(-1);
    expect(prepareMedia).toBeGreaterThan(-1);
    expect(lint).toBeGreaterThan(-1);
    expect(typecheck).toBeGreaterThan(-1);
    expect(test).toBeGreaterThan(-1);
    expect(build).toBeGreaterThan(-1);
    expect(verify).toBeGreaterThan(-1);
    expect(upload).toBeGreaterThan(-1);

    expect(npmCi).toBeGreaterThan(setupFfmpeg);
    expect(prepareMedia).toBeGreaterThan(npmCi);
    expect(lint).toBeGreaterThan(prepareMedia);
    expect(typecheck).toBeGreaterThan(lint);
    expect(test).toBeGreaterThan(typecheck);
    expect(build).toBeGreaterThan(test);
    expect(verify).toBeGreaterThan(build);
    expect(upload).toBeGreaterThan(verify);
  });

  it("stores the requested route and redirects the Pages 404 response to the app base", () => {
    expect(existsSync(pages404Path)).toBe(true);
    if (!existsSync(pages404Path)) {
      return;
    }

    const requested = new URL(
      "https://what912.github.io/VideoScope/report/demo?view=research#finding-1",
    );
    const { replace, stored } = executePages404(requested);

    expect(replace).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith(
      "https://what912.github.io/VideoScope/",
    );
    expect(JSON.parse(stored.get(pagesRouteStorageKey) ?? "{}")).toEqual({
      hash: "#finding-1",
      origin: "https://what912.github.io",
      pathname: "/VideoScope/report/demo",
      search: "?view=research",
    });
  });

  it("still redirects to the app base when session storage is unavailable", () => {
    const html = readFileSync(pages404Path, "utf8");
    const script = html.match(/<script>([\s\S]*?)<\/script>/u)?.[1];
    const replace = vi.fn();
    const redirectWindow = {
      location: {
        hash: "",
        origin: "https://what912.github.io",
        pathname: "/VideoScope/workspace",
        replace,
        search: "",
      },
      sessionStorage: {
        removeItem() {
          throw new DOMException("Storage blocked", "SecurityError");
        },
        setItem() {
          throw new DOMException("Storage blocked", "SecurityError");
        },
      },
    };

    expect(script).toBeDefined();
    expect(() => new Function("window", script ?? "")(redirectWindow)).not.toThrow();
    expect(replace).toHaveBeenCalledWith(
      "https://what912.github.io/VideoScope/",
    );
  });

  it("restores a same-origin project route before router creation and consumes the payload", async () => {
    window.sessionStorage.setItem(
      pagesRouteStorageKey,
      JSON.stringify({
        hash: "#finding-1",
        origin: window.location.origin,
        pathname: "/VideoScope/report/demo",
        search: "?view=research",
      }),
    );

    const mainModule = await importMainWithoutMounting();

    expect(window.location.pathname).toBe("/VideoScope/report/demo");
    expect(window.location.search).toBe("?view=research");
    expect(window.location.hash).toBe("#finding-1");
    expect(window.sessionStorage.getItem(pagesRouteStorageKey)).toBeNull();
    expect(
      Reflect.get(mainModule, "restorePagesRouteBeforeRouter"),
    ).toBeTypeOf("function");

    render(<TestApp initialEntries={[window.location.pathname.slice(11)]} />);
    expect(
      await screen.findByRole("heading", {
        name: "Video Observatory interactive demonstration",
      }),
    ).toBeVisible();
  });

  it.each([
    {
      name: "a different origin",
      payload: {
        hash: "",
        origin: "https://malicious.example",
        pathname: "/VideoScope/report/demo",
        search: "",
      },
    },
    {
      name: "a normalized path outside the project base",
      payload: {
        hash: "",
        origin: "http://localhost",
        pathname: "/VideoScope/../outside",
        search: "",
      },
    },
    {
      name: "a protocol-relative pathname",
      payload: {
        hash: "",
        origin: "http://localhost",
        pathname: "//malicious.example/VideoScope/report/demo",
        search: "",
      },
    },
  ])("rejects $name and removes the one-shot payload", async ({ payload }) => {
    window.sessionStorage.setItem(
      pagesRouteStorageKey,
      JSON.stringify({
        ...payload,
        origin:
          payload.origin === "http://localhost"
            ? window.location.origin
            : payload.origin,
      }),
    );

    await importMainWithoutMounting();

    expect(window.location.pathname).toBe("/VideoScope/");
    expect(window.location.search).toBe("");
    expect(window.location.hash).toBe("");
    expect(window.sessionStorage.getItem(pagesRouteStorageKey)).toBeNull();
  });

  it.each([
    {
      name: "reading",
      storage: {
        getItem() {
          throw new DOMException("Storage blocked", "SecurityError");
        },
        removeItem() {},
      },
    },
    {
      name: "consuming",
      storage: {
        getItem() {
          return JSON.stringify({
            hash: "",
            origin: window.location.origin,
            pathname: "/VideoScope/report/demo",
            search: "",
          });
        },
        removeItem() {
          throw new DOMException("Storage blocked", "SecurityError");
        },
      },
    },
  ])(
    "keeps the app at its current route when session storage fails while $name the payload",
    async ({ storage }) => {
      const mainModule = await importMainWithoutMounting();
      const replaceState = vi.fn();
      const deniedTarget = {
        history: { replaceState },
        location: {
          origin: window.location.origin,
        },
        sessionStorage: storage,
      } as unknown as Window;

      let restored: boolean | undefined;
      expect(() => {
        restored = mainModule.restorePagesRouteBeforeRouter(deniedTarget);
      }).not.toThrow();
      expect(restored).toBe(false);
      expect(replaceState).not.toHaveBeenCalled();
    },
  );
});
