import { Link } from "react-router";

import type { PublicFunnelLocaleCopy } from "../growth/growth-copy-runtime";
import { HomeMedia } from "./HomeMedia";

interface HeroProps {
  copy: PublicFunnelLocaleCopy;
  onQuickCheck(): void;
}

export function Hero({ copy, onQuickCheck }: HeroProps) {
  const labels = copy.home.hero;

  return (
    <section className="home-hero" data-testid="home-hero">
      <HomeMedia
        className="home-hero__atmosphere"
        eager
        label={labels.media}
        role="hero"
      />
      <div className="home-hero__grid" aria-hidden="true" />
      <div className="home-hero__copy">
        <p className="eyebrow">{labels.eyebrow}</p>
        <h1>{copy.positioning}</h1>
        <p className="home-hero__description">{copy.localBoundary}</p>
        <div className="home-actions">
          <Link className="button button--primary" to="/rescue">
            {labels.primaryAction}
          </Link>
          <button className="button button--quiet" onClick={onQuickCheck} type="button">
            {labels.quickCheck}
          </button>
          <Link className="text-link" to="/examples">
            {labels.examples}
          </Link>
        </div>
        <p className="home-hero__trust">{copy.sourcePreserved}</p>
      </div>
      <div aria-hidden="true" className="home-hero__scan" data-decorative-motion />
    </section>
  );
}
