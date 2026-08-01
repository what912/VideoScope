import { useI18n } from "../../i18n/I18nProvider";

export function CreatorBadge() {
  const { t } = useI18n();

  return (
    <a
      aria-label={t.brand.creatorLabel}
      className="creator-badge numeric"
      href="https://github.com/what912"
    >
      <span aria-hidden="true">↗</span>
      {t.brand.creator}
    </a>
  );
}
