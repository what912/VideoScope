import { REPOSITORY_URL } from "./growth-copy";
import { GrowthPage } from "./GrowthPage";

export function DevelopersPage() {
  return <GrowthPage actionHref={REPOSITORY_URL} page="developers" />;
}
