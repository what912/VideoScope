import { lazy, Suspense } from "react";
import {
  createBrowserRouter,
  createMemoryRouter,
  type InitialEntry,
  type RouteObject,
} from "react-router";
import { RouterProvider } from "react-router/dom";

import { LoadingState } from "../components/feedback/LoadingState";
import { HomePage } from "../features/home/HomePage";
import { App } from "./App";
import { AppErrorBoundary } from "./AppErrorBoundary";
import { AppProviders } from "./AppProviders";

const WorkspacePage = lazy(async () => {
  const module = await import("../features/workspace/WorkspacePage");
  return { default: module.WorkspacePage };
});
const ComparePage = lazy(async () => {
  const module = await import("../features/compare/ComparePage");
  return { default: module.ComparePage };
});
const ReportPage = lazy(async () => {
  const module = await import("../features/report/ReportPage");
  return { default: module.ReportPage };
});
const AuthPage = lazy(async () => {
  const module = await import("../features/auth/AuthPage");
  return { default: module.AuthPage };
});
const AuthCallbackPage = lazy(async () => {
  const module = await import("../features/auth/AuthCallbackPage");
  return { default: module.AuthCallbackPage };
});
const ConnectorPage = lazy(async () => {
  const module = await import("../features/connector/ConnectorPage");
  return { default: module.ConnectorPage };
});
const RescueLandingPage = lazy(async () => {
  const module = await import("../features/growth/RescueLandingPage");
  return { default: module.RescueLandingPage };
});
const ExamplesPage = lazy(async () => {
  const module = await import("../features/growth/ExamplesPage");
  return { default: module.ExamplesPage };
});
const CaseStudyPage = lazy(async () => {
  const module = await import("../features/growth/CaseStudyPage");
  return { default: module.CaseStudyPage };
});
const DownloadPage = lazy(async () => {
  const module = await import("../features/growth/DownloadPage");
  return { default: module.DownloadPage };
});
const DevelopersPage = lazy(async () => {
  const module = await import("../features/growth/DevelopersPage");
  return { default: module.DevelopersPage };
});
const RoadmapPage = lazy(async () => {
  const module = await import("../features/growth/RoadmapPage");
  return { default: module.RoadmapPage };
});
const CommunityPage = lazy(async () => {
  const module = await import("../features/growth/CommunityPage");
  return { default: module.CommunityPage };
});
const PrivacyPage = lazy(async () => {
  const module = await import("../features/static/PrivacyPage");
  return { default: module.PrivacyPage };
});
const DocsPage = lazy(async () => {
  const module = await import("../features/static/DocsPage");
  return { default: module.DocsPage };
});
const NotFoundPage = lazy(async () => {
  const module = await import("../features/static/NotFoundPage");
  return { default: module.NotFoundPage };
});

function LazyRoute({ component: Component }: { component: React.LazyExoticComponent<React.ComponentType> }) {
  return (
    <Suspense fallback={<LoadingState />}>
      <Component />
    </Suspense>
  );
}

const routes: RouteObject[] = [
  {
    path: "/",
    Component: App,
    errorElement: <AppErrorBoundary />,
    children: [
      { index: true, Component: HomePage },
      { path: "workspace", element: <LazyRoute component={WorkspacePage} /> },
      { path: "compare", element: <LazyRoute component={ComparePage} /> },
      { path: "report/:reportId", element: <LazyRoute component={ReportPage} /> },
      { path: "auth", element: <LazyRoute component={AuthPage} /> },
      { path: "connect", element: <LazyRoute component={ConnectorPage} /> },
      { path: "rescue", element: <LazyRoute component={RescueLandingPage} /> },
      { path: "examples", element: <LazyRoute component={ExamplesPage} /> },
      { path: "examples/:slug", element: <LazyRoute component={CaseStudyPage} /> },
      { path: "download", element: <LazyRoute component={DownloadPage} /> },
      { path: "developers", element: <LazyRoute component={DevelopersPage} /> },
      { path: "roadmap", element: <LazyRoute component={RoadmapPage} /> },
      { path: "community", element: <LazyRoute component={CommunityPage} /> },
      {
        path: "auth/callback",
        element: <LazyRoute component={AuthCallbackPage} />,
      },
      { path: "privacy", element: <LazyRoute component={PrivacyPage} /> },
      { path: "docs", element: <LazyRoute component={DocsPage} /> },
      { path: "*", element: <LazyRoute component={NotFoundPage} /> },
    ],
  },
];

export function createAppRouter(initialEntries?: InitialEntry[]) {
  return initialEntries
    ? createMemoryRouter(routes, { initialEntries })
    : createBrowserRouter(routes, { basename: "/VideoScope" });
}

export function AppRouter() {
  return <RouterProvider router={createAppRouter()} />;
}

export function TestApp({ initialEntries }: { initialEntries: InitialEntry[] }) {
  return (
    <AppProviders>
      <RouterProvider router={createAppRouter(initialEntries)} />
    </AppProviders>
  );
}
