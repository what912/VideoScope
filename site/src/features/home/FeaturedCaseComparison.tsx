import { useRef, useState } from "react";

import type { CaseStudy } from "../../data/case-studies";
import { useI18n } from "../../i18n/I18nProvider";
import type { HomeCopy } from "../growth/growth-copy-runtime";

const SYNC_TOLERANCE_SECONDS = 0.08;

function syncPosition(source: HTMLVideoElement, target: HTMLVideoElement | null) {
  if (target && Math.abs(target.currentTime - source.currentTime) > SYNC_TOLERANCE_SECONDS) {
    target.currentTime = source.currentTime;
  }
}

export function FeaturedCaseComparison({
  copy,
  item,
}: {
  copy: HomeCopy["comparison"];
  item: CaseStudy;
}) {
  const { locale } = useI18n();
  const before = useRef<HTMLVideoElement>(null);
  const after = useRef<HTMLVideoElement>(null);
  const range = useRef<HTMLInputElement>(null);
  const [playing, setPlaying] = useState(false);
  const duration = item.comparison.endSeconds - item.comparison.startSeconds;
  const syncBoth = (time: number) => {
    if (range.current) range.current.value = String(time);
    for (const video of [before.current, after.current]) {
      if (video && Math.abs(video.currentTime - time) > SYNC_TOLERANCE_SECONDS) {
        video.currentTime = time;
      }
    }
  };

  const handleTimeUpdate = (source: HTMLVideoElement) => {
    if (range.current) range.current.value = String(source.currentTime);
    syncPosition(source, after.current);
  };

  const playBoth = () => {
    setPlaying(true);
    for (const video of [before.current, after.current]) {
      void video?.play().catch(() => setPlaying(false));
    }
  };

  const pauseBoth = () => {
    setPlaying(false);
    before.current?.pause();
    after.current?.pause();
  };

  return (
    <section className="featured-case" data-testid="featured-case-comparison" aria-labelledby="featured-case-title">
      <div className="featured-case__heading">
        <p className="eyebrow">{copy.eyebrow}</p>
        <h2 id="featured-case-title">{item.title[locale]}</h2>
        <p>{item.summary[locale]}</p>
        <p className="demo-label">{copy.authored}</p>
      </div>
      <div className="featured-case__players">
        <figure>
          <video
            aria-label={copy.before}
            muted
            onPause={() => setPlaying(false)}
            onTimeUpdate={(event) => handleTimeUpdate(event.currentTarget)}
            playsInline
            poster={item.assets.poster}
            preload="metadata"
            ref={before}
            src={item.assets.beforeVideo}
          />
          <figcaption>{copy.before}</figcaption>
        </figure>
        <figure>
          <video
            aria-label={copy.after}
            muted
            playsInline
            poster={item.assets.poster}
            preload="metadata"
            ref={after}
            src={item.assets.afterVideo}
          />
          <figcaption>{copy.after}</figcaption>
        </figure>
      </div>
      <div className="featured-case__controls">
        <label>
          <span>{copy.position}</span>
          <input
            aria-label={copy.position}
            max={duration}
            min="0"
            onChange={(event) => syncBoth(Number(event.currentTarget.value))}
            ref={range}
            step="0.01"
            type="range"
          />
        </label>
        <button className="button button--quiet" onClick={playing ? pauseBoth : playBoth} type="button">
          {playing ? copy.pause : copy.play}
        </button>
      </div>
      <dl className="featured-case__facts">
        <div>
          <dt>{copy.range}</dt>
          <dd>{item.comparison.startSeconds}s–{item.comparison.endSeconds}s</dd>
        </div>
        <div>
          <dt>{copy.verification}</dt>
          <dd>{item.verification.status}</dd>
        </div>
      </dl>
      <section aria-label={copy.limitations}>
        <h3>{copy.limitations}</h3>
        <ul>
          {item.limitations.map((limitation) => <li key={limitation.en}>{limitation[locale]}</li>)}
        </ul>
      </section>
    </section>
  );
}
