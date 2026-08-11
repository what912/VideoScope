import { REPOSITORY_URL } from "./growth-constants";
import { GrowthPage } from "./GrowthPage";

export function DevelopersPage() {
  return <GrowthPage actionHref={REPOSITORY_URL} page="developers" />;
}
