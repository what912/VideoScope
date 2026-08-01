import { ViewportVideo } from "../../components/media/ViewportVideo";
import type { HomepageMediaRole } from "../../data/media-manifest";
import type { Ref, SyntheticEvent } from "react";
import { mediaFor } from "./home-data";

interface HomeMediaProps {
  autoPlayOnIntersection?: boolean;
  className?: string;
  eager?: boolean;
  label: string;
  mediaRef?: Ref<HTMLVideoElement>;
  onLoadedMetadata?: (event: SyntheticEvent<HTMLVideoElement>) => void;
  onTimeUpdate?: (event: SyntheticEvent<HTMLVideoElement>) => void;
  onViewportExit?: () => void;
  role: HomepageMediaRole;
}
export function HomeMedia({
  autoPlayOnIntersection,
  className,
  eager,
  label,
  mediaRef,
  onLoadedMetadata,
  onTimeUpdate,
  onViewportExit,
  role,
}: HomeMediaProps) {
  const media = mediaFor(role);
  const asset = media.video ?? media.poster;
  return (
    <div
      className={className}
      data-asset={asset}
      data-media-role={role}
      data-testid="home-media-role"
    >
      {media.video ? (
        <ViewportVideo
          autoPlayOnIntersection={autoPlayOnIntersection}
          className="home-media__asset"
          eager={eager}
          label={label}
          mediaRef={mediaRef}
          onLoadedMetadata={onLoadedMetadata}
          onTimeUpdate={onTimeUpdate}
          onViewportExit={onViewportExit}
          poster={media.poster}
          src={media.video}
        />
      ) : (
        <img
          alt={label}
          className="home-media__asset"
          height={270}
          loading="lazy"
          src={media.poster}
          width={480}
        />
      )}
    </div>
  );
}
