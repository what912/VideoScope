import { REPOSITORY_URL } from "./growth-constants";
import { GrowthPage } from "./GrowthPage";

export function CommunityPage() {
  return <GrowthPage actionHref={REPOSITORY_URL} page="community" />;
}
