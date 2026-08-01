import { useI18n } from "../../i18n/I18nProvider";

type LoadingStateProps = {
  message?: string;
  title?: string;
};

export function LoadingState({ message, title }: LoadingStateProps) {
  const { t } = useI18n();

  return (
    <section
      aria-label={t.feedback.loadingLabel}
      aria-live="polite"
      className="feedback-state feedback-state--loading"
      role="status"
    >
      <span aria-hidden="true" className="feedback-state__symbol">
        ↻
      </span>
      <h2>{title ?? t.feedback.loadingTitle}</h2>
      <p>{message ?? t.feedback.loadingMessage}</p>
    </section>
  );
}
