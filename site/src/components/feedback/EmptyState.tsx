import type { ReactNode } from "react";

import { useI18n } from "../../i18n/I18nProvider";

type EmptyStateProps = {
  action?: ReactNode;
  message?: string;
  title?: string;
};

export function EmptyState({ action, message, title }: EmptyStateProps) {
  const { t } = useI18n();

  return (
    <section
      aria-label={t.feedback.emptyLabel}
      className="feedback-state feedback-state--empty"
    >
      <span aria-hidden="true" className="feedback-state__symbol">
        ○
      </span>
      <h2>{title ?? t.feedback.emptyTitle}</h2>
      <p>{message ?? t.feedback.emptyMessage}</p>
      {action ? <div className="feedback-state__action">{action}</div> : null}
    </section>
  );
}
