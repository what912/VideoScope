import { REPOSITORY_URL } from "./growth-constants";
import { GrowthPage } from "./GrowthPage";

export function RoadmapPage() {
  return <GrowthPage actionHref={`${REPOSITORY_URL}/blob/main/docs/roadmap.md`} page="roadmap" />;
}
