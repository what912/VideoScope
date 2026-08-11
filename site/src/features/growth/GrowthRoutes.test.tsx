import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { TestApp } from "../../app/router";

describe("public growth routes", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it.each([
    "/rescue",
    "/examples",
    "/examples/timeline-rescue",
    "/download",
    "/developers",
    "/roadmap",
    "/community",
  ])("renders %s without authentication", async (path) => {
    render(<TestApp initialEntries={[path]} />);

    expect(await screen.findByRole("main")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", {
        name: "This route is outside the observatory",
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Created by what912")).toBeInTheDocument();
    expect(screen.queryByText(/sign in to continue/iu)).not.toBeInTheDocument();
  });

  it("keeps attribution unchanged after locale switch", async () => {
    render(<TestApp initialEntries={["/rescue"]} />);

    const language = await screen.findByRole("combobox", { name: "Language" });
    fireEvent.change(language, { target: { value: "zh-CN" } });

    expect(screen.getByText("Created by what912")).toBeInTheDocument();
  });
});
