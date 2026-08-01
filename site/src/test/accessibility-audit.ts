const interactiveSelector = [
  "a[href]",
  "button",
  "input:not([type='hidden'])",
  "select",
  "textarea",
  "[role='button']",
  "[role='link']",
].join(",");

function referencedText(element: Element, attribute: string) {
  return (element.getAttribute(attribute) ?? "")
    .split(/\s+/u)
    .filter(Boolean)
    .map((id) => document.getElementById(id)?.textContent?.trim() ?? "")
    .filter(Boolean)
    .join(" ");
}

export function accessibleName(element: HTMLElement) {
  const ariaLabel = element.getAttribute("aria-label")?.trim();
  if (ariaLabel) return ariaLabel;
  const labelledBy = referencedText(element, "aria-labelledby");
  if (labelledBy) return labelledBy;
  if (
    element instanceof HTMLInputElement ||
    element instanceof HTMLSelectElement ||
    element instanceof HTMLTextAreaElement
  ) {
    const labels = Array.from(element.labels ?? [])
      .map((label) => label.textContent?.trim() ?? "")
      .filter(Boolean)
      .join(" ");
    if (labels) return labels;
  }
  if (element instanceof HTMLImageElement) return element.alt.trim();
  return element.textContent?.trim() ?? "";
}

function describe(element: Element) {
  const id = element.id ? `#${element.id}` : "";
  return `${element.tagName.toLowerCase()}${id}`;
}

export interface AccessibilityAuditOptions {
  requirePageHeading?: boolean;
  requireSiteLandmarks?: boolean;
}

export function auditAccessibility(
  container: HTMLElement,
  {
    requirePageHeading = true,
    requireSiteLandmarks = false,
  }: AccessibilityAuditOptions = {},
) {
  const issues: string[] = [];
  const elements = [container, ...container.querySelectorAll<HTMLElement>("*")];
  const ids = new Map<string, number>();
  for (const element of elements) {
    if (element.id) ids.set(element.id, (ids.get(element.id) ?? 0) + 1);
  }
  for (const [id, count] of ids) {
    if (count > 1) issues.push(`duplicate id #${id}`);
  }

  for (const element of elements) {
    for (const attribute of [
      "aria-controls",
      "aria-describedby",
      "aria-labelledby",
      "aria-owns",
    ]) {
      for (const id of (element.getAttribute(attribute) ?? "")
        .split(/\s+/u)
        .filter(Boolean)) {
        if (!container.querySelector(`#${CSS.escape(id)}`)) {
          issues.push(`${describe(element)} has missing ${attribute} #${id}`);
        }
      }
    }
  }

  const headings = [...container.querySelectorAll("h1,h2,h3,h4,h5,h6")];
  if (requirePageHeading) {
    if (headings.filter((heading) => heading.tagName === "H1").length !== 1) {
      issues.push("page must contain exactly one h1");
    }
    if (headings[0]?.tagName !== "H1") {
      issues.push("first page heading must be h1");
    }
  }

  if (requireSiteLandmarks) {
    if (!container.querySelector(".site-header")) {
      issues.push("site banner is missing");
    }
    if (container.querySelectorAll("main").length !== 1) {
      issues.push("site must contain exactly one main landmark");
    }
    if (container.querySelectorAll("footer").length !== 1) {
      issues.push("site footer is missing");
    }
  }
  if (container.querySelector("main main")) {
    issues.push("main landmarks must not be nested");
  }

  for (const element of container.querySelectorAll<HTMLElement>(
    `${interactiveSelector}, [role='dialog'], [role='alertdialog'], nav`,
  )) {
    if (!accessibleName(element)) {
      issues.push(`${describe(element)} has no accessible name`);
    }
  }

  for (const control of container.querySelectorAll<
    HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
  >("input:not([type='hidden']), select, textarea")) {
    if (!accessibleName(control)) {
      issues.push(`${describe(control)} has no form label`);
    }
  }

  for (const image of container.querySelectorAll("img")) {
    if (!image.hasAttribute("alt")) {
      issues.push(`${describe(image)} has no alt attribute`);
    }
  }

  for (const interactive of container.querySelectorAll(interactiveSelector)) {
    if (interactive.querySelector(interactiveSelector)) {
      issues.push(`${describe(interactive)} nests an interactive control`);
    }
  }
  for (const element of elements) {
    if (element.tabIndex > 0) {
      issues.push(`${describe(element)} uses positive tabindex`);
    }
    if (
      ["button", "link"].includes(element.getAttribute("role") ?? "") &&
      element.tabIndex < 0
    ) {
      issues.push(`${describe(element)} has a non-focusable interactive role`);
    }
  }

  for (const dialog of container.querySelectorAll<HTMLElement>(
    "[role='dialog'], [role='alertdialog']",
  )) {
    if (dialog.getAttribute("aria-modal") !== "true") {
      issues.push(`${describe(dialog)} must declare aria-modal=true`);
    }
  }

  for (const severity of container.querySelectorAll<HTMLElement>(
    "[data-severity]",
  )) {
    if (severity.getAttribute("aria-hidden") === "true") continue;
    if (!accessibleName(severity)) {
      issues.push(`${describe(severity)} exposes severity by color only`);
    }
  }

  return issues;
}
