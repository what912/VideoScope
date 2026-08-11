import { REPOSITORY_URL } from "./growth-copy";
import { GrowthPage } from "./GrowthPage";

export function RoadmapPage() {
  return <GrowthPage actionHref={`${REPOSITORY_URL}/blob/main/docs/roadmap.md`} page="roadmap" />;
}
