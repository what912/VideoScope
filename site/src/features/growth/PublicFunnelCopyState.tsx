import { useI18n } from "../../i18n/I18nProvider";
import { publicFunnelCopyStatus } from "./growth-constants";
import type { PublicFunnelCopyState as CopyState } from "./use-public-funnel-copy";

export function PublicFunnelCopyState({ state }: { state: Exclude<CopyState, { status: "ready" }> }) {
  const { locale } = useI18n();
  const message = state.status === "loading"
    ? publicFunnelCopyStatus[locale].loading
    : publicFunnelCopyStatus[locale].unavailable;

  return (
    <section aria-live="polite" className="growth-page" role="status">
      <h1>{message}</h1>
    </section>
  );
}
