import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

import { useI18n } from "../../i18n/I18nProvider";

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface WorkspaceConfirmDialogProps {
  busy: boolean;
  error?: string;
  onCancel(): void;
  onConfirm(): void;
}

export function WorkspaceConfirmDialog({
  busy,
  error,
  onCancel,
  onConfirm,
}: WorkspaceConfirmDialogProps) {
  const { t } = useI18n();
  const titleId = useId();
  const cancelButton = useRef<HTMLButtonElement>(null);

  useEffect(() => cancelButton.current?.focus(), []);

  return createPortal(
    <div className="workspace-modal-backdrop">
      <div
        aria-labelledby={titleId}
        aria-modal="true"
        className="workspace-confirm-dialog"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !busy) {
            event.preventDefault();
            onCancel();
            return;
          }
          if (event.key !== "Tab") return;
          const focusable = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              FOCUSABLE_SELECTOR,
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
        <p className="eyebrow">{t.workspace.clearDialogEyebrow}</p>
        <h2 id={titleId}>{t.workspace.clearDialogTitle}</h2>
        <p>{t.workspace.clearDialogMessage}</p>
        {error ? (
          <p className="workspace-confirm-dialog__error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="workspace-confirm-dialog__actions">
          <button
            className="button button--quiet"
            disabled={busy}
            onClick={onCancel}
            ref={cancelButton}
            type="button"
          >
            {t.workspace.cancel}
          </button>
          <button
            className="button button--danger"
            disabled={busy}
            onClick={onConfirm}
            type="button"
          >
            {busy ? t.workspace.clearingLocalData : t.workspace.clearLocalData}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
