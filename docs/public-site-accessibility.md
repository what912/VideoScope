# Public site accessibility verification

VideoScope treats accessibility as a release gate for the public browser
experience. The repository currently runs a dependency-free DOM audit over
every public route plus populated Workspace, Compare, Report, destructive
confirmation, sharing, and mobile-navigation states.

The automated audit checks:

- one page-level heading and valid landmark structure;
- accessible names for navigation, dialogs, controls, and form inputs;
- valid ARIA references and modal declarations;
- image alternatives;
- duplicate identifiers;
- nested interactive controls and invalid positive tab order;
- severity information that is not exposed by color alone.

Run the complete browser gate from `site/`:

```powershell
npm run check
```

This gate also builds the production bundle, validates local media, rejects
unapproved remote URLs in built HTML/CSS/JavaScript, and enforces bundle-size
budgets.

## Final Task 16 release gate

The dependency-free audit is deliberately not described as an axe scan. Before
the public release is approved, Task 16 must additionally perform:

1. an external `axe-core` or equivalent browser scan after that dependency is
   explicitly approved;
2. keyboard-only navigation of all routes, dialogs, upload, timeline, compare,
   sharing, and local-data deletion;
3. focus-order and focus-return checks for every dialog;
4. light/dark contrast checks for text, focus rings, charts, severity labels,
   and disabled controls;
5. reduced-motion verification with the operating-system preference enabled;
6. at least one screen-reader pass over upload progress, errors, timeline
   findings, report summaries, and offline service messaging;
7. desktop, tablet, and narrow-mobile viewport checks with no horizontal
   overflow.

Record browser, operating system, viewport, assistive technology, failures, and
the person performing the manual check in the final release audit. A failed or
unperformed item remains a release blocker; it must not be reported as passed.
