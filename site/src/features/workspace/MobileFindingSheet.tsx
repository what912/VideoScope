import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { IssueDetailPanel } from "../../components/diagnostics";
import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";

interface MobileFindingSheetProps {
  finding: Finding;
  onClose(): void;
  onEvidenceSeek(timestampSeconds: number): void;
}

export function MobileFindingSheet({
  finding,
  onClose,
  onEvidenceSeek,
}: MobileFindingSheetProps) {
  const { t } = useI18n();
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => closeButton.current?.focus(), []);

  return createPortal(
    <div className="workspace-modal-backdrop workspace-modal-backdrop--sheet">
      <div
        aria-modal="true"
        aria-label={t.diagnostics.findingDetails}
        className="workspace__bottom-sheet"
        data-presentation="bottom-sheet"
        onKeyDown={(event) => {
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
        role="dialog"
      >
        <button
          aria-label={t.workspace.closeDetails}
          className="icon-button workspace__sheet-close"
          onClick={onClose}
          ref={closeButton}
          type="button"
        >
          ×
        </button>
        <IssueDetailPanel
          finding={finding}
          onEvidenceSeek={onEvidenceSeek}
        />
      </div>
    </div>,
    document.body,
  );
}
