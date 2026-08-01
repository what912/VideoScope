import { useEffect, useState } from "react";

type Theme = "light" | "dark";

function initialTheme(): Theme {
  const stored = window.localStorage.getItem("videoscope-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function Header(): React.JSX.Element {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("videoscope-theme", theme);
  }, [theme]);

  return (
    <header className="site-header">
      <a className="brand" href="/" aria-label="VideoScope home">
        <span className="brand-mark" aria-hidden="true">
          VS
        </span>
        <span>VideoScope</span>
      </a>
      <div className="header-meta">
        <span className="local-badge">
          <span className="status-dot" aria-hidden="true" />
          Local session
        </span>
        <button
          className="icon-button"
          type="button"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
        >
          <span aria-hidden="true">{theme === "dark" ? "☀" : "◐"}</span>
        </button>
      </div>
    </header>
  );
}
