import { lazy, Suspense, useRef } from "react";
import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { CREATOR_ATTRIBUTION, REPOSITORY_URL } from "../growth/growth-constants";
import { PublicFunnelCopyState } from "../growth/PublicFunnelCopyState";
import { usePublicFunnelCopy } from "../growth/use-public-funnel-copy";
import { FinalCta } from "./FinalCta";
import { Hero } from "./Hero";
import { HomeUploadLab } from "./HomeUploadLab";
import "./home.css";

const HomeCaseEvidence = lazy(async () => {
  const module = await import("./HomeCaseEvidence");
  return { default: module.HomeCaseEvidence };
});

export function HomePage() {
  const { locale } = useI18n();
  const copyState = usePublicFunnelCopy();
  const uploadRef = useRef<HTMLElement>(null);

  const focusUploadLab = () => {
    const reducedMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    uploadRef.current?.scrollIntoView({
      behavior: reducedMotion ? "auto" : "smooth",
      block: "start",
    });
    uploadRef.current?.focus({ preventScroll: true });
  };

  if (copyState.status !== "ready") {
    return <div className="home-page"><PublicFunnelCopyState state={copyState} /></div>;
  }

  const copy = copyState.copy[locale];

  return (
    <div className="home-page">
      <Hero copy={copy} onQuickCheck={focusUploadLab} />
      <Suspense fallback={<section className="home-section funnel-case-loading" role="status" aria-live="polite"><p>{copy.home.cases.loading}</p></section>}>
        <HomeCaseEvidence copy={copy.home} />
      </Suspense>
      <section className="home-section funnel-journey" aria-labelledby="creator-journey-title">
        <p className="eyebrow">{copy.home.funnel.journeyEyebrow}</p>
        <h2 id="creator-journey-title">{copy.home.funnel.journeyTitle}</h2>
        <ol>
          {copy.home.funnel.journey.map((step, index) => (
            <li key={step.title}>
              <span className="numeric">0{index + 1}</span>
              <h3>{step.title}</h3>
              <p>{step.description}</p>
            </li>
          ))}
        </ol>
      </section>
      <section className="home-section funnel-boundary" aria-labelledby="funnel-boundary-title">
        <h2 id="funnel-boundary-title">{copy.home.funnel.boundaryTitle}</h2>
        <p>{copy.home.funnel.boundaryDescription}</p>
        <p>{copy.sourcePreserved}</p>
      </section>
      <section
        className="home-anchor"
        data-testid="home-upload-lab"
        ref={uploadRef}
        tabIndex={-1}
      >
        <HomeUploadLab atmosphereLabel={copy.home.uploadAtmosphere} />
      </section>
      <FinalCta copy={copy.home.finalCta} />
      <section className="home-section funnel-developer" aria-labelledby="funnel-developer-title">
        <h2 id="funnel-developer-title">{copy.home.funnel.developerTitle}</h2>
        <p>{copy.home.funnel.developerDescription}</p>
        <Link className="text-link" to="/developers">{copy.home.funnel.developerAction}</Link>
      </section>
      <section aria-label={CREATOR_ATTRIBUTION} className="funnel-attribution">
        <p>{CREATOR_ATTRIBUTION}</p>
        <a className="text-link" href={REPOSITORY_URL}>{copy.home.funnel.star}</a>
      </section>
    </div>
  );
}
