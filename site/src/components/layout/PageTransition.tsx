import type { PropsWithChildren } from "react";
import { useLocation } from "react-router";

export function PageTransition({ children }: PropsWithChildren) {
  const location = useLocation();

  return (
    <div className="page-transition" key={location.pathname}>
      {children}
    </div>
  );
}
