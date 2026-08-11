import { REPOSITORY_URL } from "./growth-copy";
import { GrowthPage } from "./GrowthPage";

export function CommunityPage() {
  return <GrowthPage actionHref={REPOSITORY_URL} page="community" />;
}
