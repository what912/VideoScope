import { useI18n } from "../../i18n/I18nProvider";

type ErrorStateProps = {
  message?: string;
  onRetry?(): void;
  title?: string;
};

export function ErrorState({ message, onRetry, title }: ErrorStateProps) {
  const { t } = useI18n();

  return (
    <section
      aria-label={t.feedback.errorLabel}
      className="feedback-state feedback-state--error"
      role="alert"
    >
      <span aria-hidden="true" className="feedback-state__symbol">
        !
      </span>
      <h2>{title ?? t.feedback.errorTitle}</h2>
      <p>{message ?? t.feedback.errorMessage}</p>
      {onRetry ? (
        <button className="button button--quiet" onClick={onRetry} type="button">
          {t.feedback.retry}
        </button>
      ) : null}
    </section>
  );
}
