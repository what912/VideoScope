import {
  type Ref,
  type SyntheticEvent,
  useEffect,
  useRef,
  useState,
} from "react";

interface ViewportVideoProps {
  readonly autoPlayOnIntersection?: boolean;
  readonly className?: string;
  readonly eager?: boolean;
  readonly label: string;
  readonly mediaRef?: Ref<HTMLVideoElement>;
  readonly onLoadedMetadata?: (event: SyntheticEvent<HTMLVideoElement>) => void;
  readonly onTimeUpdate?: (event: SyntheticEvent<HTMLVideoElement>) => void;
  readonly onViewportExit?: () => void;
  readonly poster: string;
  readonly src: string;
}

const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function prefersReducedMotion() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(REDUCED_MOTION_QUERY).matches
  );
}

function prefersDataSaver() {
  const connection = (
    navigator as Navigator & {
      readonly connection?: { readonly saveData?: boolean };
    }
  ).connection;
  return connection?.saveData === true;
}

function setRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (typeof ref === "function") {
    ref(value);
  } else if (ref) {
    ref.current = value;
  }
}

export function ViewportVideo({
  autoPlayOnIntersection = true,
  className,
  eager = false,
  label,
  mediaRef,
  onLoadedMetadata,
  onTimeUpdate,
  onViewportExit,
  poster,
  src,
}: ViewportVideoProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [reducedMotion, setReducedMotion] = useState(prefersReducedMotion);
  const [activated, setActivated] = useState(eager);
  const [intersecting, setIntersecting] = useState(eager);
  const dataSaver = prefersDataSaver();

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return;
    }

    const mediaQuery = window.matchMedia(REDUCED_MOTION_QUERY);
    const handleChange = (event: MediaQueryListEvent) => {
      setReducedMotion(event.matches);
    };
    mediaQuery.addEventListener("change", handleChange);
    setReducedMotion(mediaQuery.matches);

    return () => {
      mediaQuery.removeEventListener("change", handleChange);
    };
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (video === null) {
      return;
    }

    video.setAttribute("muted", "");

    let observer: IntersectionObserver | undefined;
    if (!reducedMotion && !dataSaver && "IntersectionObserver" in window) {
      observer = new IntersectionObserver(([entry]) => {
        if (entry?.isIntersecting) {
          setActivated(true);
          setIntersecting(true);
        } else {
          setIntersecting(false);
          onViewportExit?.();
        }
      });
      observer.observe(video);
    } else if (!dataSaver && !reducedMotion) {
      setActivated(true);
      setIntersecting(true);
    }

    return () => {
      observer?.disconnect();
      video.pause();
    };
  }, [dataSaver, onViewportExit, reducedMotion]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !activated) return;
    if (!intersecting) {
      video.pause();
    } else if (autoPlayOnIntersection) {
      void video.play().catch(() => undefined);
    }
  }, [activated, autoPlayOnIntersection, intersecting]);

  if (reducedMotion || dataSaver) {
    return (
      <img
        alt={label}
        className={className}
        loading={eager ? "eager" : "lazy"}
        src={poster}
      />
    );
  }

  return (
    <video
      aria-label={label}
      className={className}
      loop
      muted
      playsInline
      poster={poster}
      preload={eager ? "metadata" : "none"}
      ref={(element) => {
        videoRef.current = element;
        setRef(mediaRef, element);
      }}
      src={activated ? src : undefined}
      onLoadedMetadata={onLoadedMetadata}
      onTimeUpdate={onTimeUpdate}
    />
  );
}
