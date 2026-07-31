import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { useI18n } from "../../i18n/I18nProvider";
import type { ReportIndexEntry } from "../../services/report-store/report-store";

interface WorkspaceProjectRailProps {
  activeReportId: string;
  entries: ReportIndexEntry[];
  isMobile: boolean;
  onClose(): void;
  onSelect(reportId: string): void;
}

export function WorkspaceProjectRail({
  activeReportId,
  entries,
  isMobile,
  onClose,
  onSelect,
}: WorkspaceProjectRailProps) {
  const { t } = useI18n();
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (isMobile) closeButton.current?.focus();
  }, [isMobile]);

  const rail = (
    <aside
      aria-modal={isMobile ? "true" : undefined}
      aria-label={t.workspace.projectRail}
      className="workspace__rail"
      data-presentation={isMobile ? "drawer" : "rail"}
      onKeyDown={(event) => {
        if (!isMobile) return;
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
          return;
        }
        if (event.key !== "Tab") return;
        const focusable = Array.from(
          event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        );
        const first = focusable[0];
        const last = focusable.at(-1);
        if (!first || !last) return;
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }}
      role={isMobile ? "dialog" : undefined}
    >
      <div className="workspace__rail-heading">
        <h2>{t.workspace.savedReports}</h2>
        {isMobile ? (
          <button
            aria-label={t.workspace.closeProjects}
            className="icon-button"
            onClick={onClose}
            ref={closeButton}
            type="button"
          >
            ×
          </button>
        ) : null}
      </div>
      <ul>
        {entries.map((entry) => (
          <li key={entry.id}>
            <button
              aria-current={
                entry.id === activeReportId ? "page" : undefined
              }
              onClick={() => onSelect(entry.id)}
              type="button"
            >
              <strong>{entry.title}</strong>
              <span className="numeric">
                {entry.finding_count} {t.workspace.intervalsShort}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
  return isMobile
    ? createPortal(
        <div className="workspace-modal-backdrop workspace-modal-backdrop--drawer">
          {rail}
        </div>,
        document.body,
      )
    : rail;
}
