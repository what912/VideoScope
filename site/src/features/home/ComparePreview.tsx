import {
  type SyntheticEvent,
  useCallback,
  useRef,
  useState,
} from "react";
import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { HomeMedia } from "./HomeMedia";
import { legacyHomeCopy } from "./legacy-home-copy";

export function ComparePreview() {
  const { locale } = useI18n();
  const copy = legacyHomeCopy[locale];
  const [position, setPosition] = useState(42);
  const [playing, setPlaying] = useState(false);
  const firstVideoRef = useRef<HTMLVideoElement>(null);
  const secondVideoRef = useRef<HTMLVideoElement>(null);
  const positionRef = useRef(position);

  const videos = useCallback(
    () =>
      [firstVideoRef.current, secondVideoRef.current].filter(
        (video): video is HTMLVideoElement => video !== null,
      ),
    [],
  );

  const seekBoth = (nextPosition: number) => {
    positionRef.current = nextPosition;
    setPosition(nextPosition);
    const ratio = nextPosition / 100;
    videos().forEach((video) => {
      if (Number.isFinite(video.duration) && video.duration > 0) {
        video.currentTime = video.duration * ratio;
      }
    });
  };

  const applyRequestedSeek = (event: SyntheticEvent<HTMLVideoElement>) => {
    const video = event.currentTarget;
    if (Number.isFinite(video.duration) && video.duration > 0) {
      video.currentTime = video.duration * (positionRef.current / 100);
    }
  };

  const synchronizeFromFirst = (
    event: SyntheticEvent<HTMLVideoElement>,
  ) => {
    const source = event.currentTarget;
    if (!Number.isFinite(source.duration) || source.duration <= 0) return;
    const nextPosition = (source.currentTime / source.duration) * 100;
    positionRef.current = nextPosition;
    setPosition(nextPosition);
    const peer = secondVideoRef.current;
    if (
      peer &&
      Number.isFinite(peer.duration) &&
      peer.duration > 0 &&
      Math.abs(peer.currentTime - peer.duration * (nextPosition / 100)) > 0.1
    ) {
      peer.currentTime = peer.duration * (nextPosition / 100);
    }
  };

  const pauseBoth = useCallback(() => {
    videos().forEach((video) => video.pause());
    setPlaying(false);
  }, [videos]);

  const togglePlayback = async () => {
    const previewVideos = videos();
    if (playing) {
      pauseBoth();
      return;
    }
    if (previewVideos.length !== 2) {
      return;
    }
    setPlaying(true);
    try {
      await Promise.all(previewVideos.map((video) => video.play()));
    } catch {
      previewVideos.forEach((video) => video.pause());
      setPlaying(false);
    }
  };

  return (
    <section className="home-section compare-preview">
      <div className="home-section__heading">
        <p className="eyebrow">{copy.compare.eyebrow}</p>
        <h2>{copy.compare.title}</h2>
        <p>{copy.compare.description}</p>
        <span className="demo-label">{copy.demoLabel}</span>
      </div>
      <div className="compare-preview__players">
        <figure>
          <HomeMedia
            autoPlayOnIntersection={false}
            label={copy.compare.a}
            mediaRef={firstVideoRef}
            onLoadedMetadata={applyRequestedSeek}
            onTimeUpdate={synchronizeFromFirst}
            onViewportExit={pauseBoth}
            role="compare-a"
          />
          <figcaption>{copy.compare.a}</figcaption>
        </figure>
        <figure>
          <HomeMedia
            autoPlayOnIntersection={false}
            label={copy.compare.b}
            mediaRef={secondVideoRef}
            onLoadedMetadata={applyRequestedSeek}
            onViewportExit={pauseBoth}
            role="compare-b"
          />
          <figcaption>{copy.compare.b}</figcaption>
        </figure>
      </div>
      <label className="compare-preview__scrubber">
        <span>{copy.compare.sync}</span>
        <input
          aria-label={copy.compare.sync}
          max="100"
          min="0"
          onChange={(event) => seekBoth(Number(event.currentTarget.value))}
          type="range"
          value={position}
        />
        <output className="numeric">{position}%</output>
      </label>
      <div className="compare-preview__timeline" aria-hidden="true">
        <span style={{ width: `${position}%` }} />
      </div>
      <div className="compare-preview__actions">
        <button
          className="button button--quiet"
          onClick={() => void togglePlayback()}
          type="button"
        >
          {playing ? copy.compare.pause : copy.compare.play}
        </button>
        <Link className="button button--quiet" to="/compare">
          {copy.compare.open}
        </Link>
      </div>
    </section>
  );
}
