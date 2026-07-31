export type HomepageMediaRole =
  | "hero"
  | "product-proof"
  | "upload-lab"
  | "diagnosis"
  | "compare-a"
  | "compare-b"
  | "evidence-a"
  | "evidence-b"
  | "evidence-c";

export interface HomepageMedia {
  readonly role: HomepageMediaRole;
  readonly poster: string;
  readonly video?: string;
}

function mediaPath(filename: string) {
  const { BASE_URL } = (
    import.meta as ImportMeta & { readonly env: { readonly BASE_URL: string } }
  ).env;
  return `${BASE_URL}media/${filename}`;
}

export const homepageMedia: readonly HomepageMedia[] = [
  {
    role: "hero",
    video: mediaPath("hero-optical.mp4"),
    poster: mediaPath("hero-optical.webp"),
  },
  {
    role: "product-proof",
    video: mediaPath("city-nightlife.mp4"),
    poster: mediaPath("city-nightlife.webp"),
  },
  {
    role: "upload-lab",
    video: mediaPath("upload-liquid.mp4"),
    poster: mediaPath("upload-liquid.webp"),
  },
  {
    role: "diagnosis",
    video: mediaPath("diagnosis-fashion.mp4"),
    poster: mediaPath("diagnosis-fashion.webp"),
  },
  {
    role: "compare-a",
    video: mediaPath("compare-hills.mp4"),
    poster: mediaPath("compare-hills.webp"),
  },
  {
    role: "compare-b",
    video: mediaPath("compare-sunrise.mp4"),
    poster: mediaPath("compare-sunrise.webp"),
  },
  {
    role: "evidence-a",
    poster: mediaPath("evidence-lake.webp"),
  },
  {
    role: "evidence-b",
    poster: mediaPath("evidence-city.webp"),
  },
  {
    role: "evidence-c",
    poster: mediaPath("evidence-studio.webp"),
  },
];
