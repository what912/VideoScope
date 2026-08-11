import manifestUrl from "./case-studies.json?url";
import type { CaseStudy, CaseStudyManifest } from "./case-studies";

export async function loadCaseStudyManifest(): Promise<CaseStudyManifest> {
  const response = await fetch(manifestUrl);
  if (!response.ok) {
    throw new Error("The local case manifest could not be loaded.");
  }
  return response.json() as Promise<CaseStudyManifest>;
}

export async function loadFeaturedCaseStudies(): Promise<readonly CaseStudy[]> {
  const manifest = await loadCaseStudyManifest();
  return manifest.cases.filter(
    (item) => item.featured && item.verification.status === "completed",
  );
}
