import { useEffect, useState } from "react";

import {
  loadPublicFunnelCopy,
  type PublicFunnelCopy,
} from "./growth-copy-runtime";

export type PublicFunnelCopyState =
  | { readonly status: "loading" }
  | { readonly status: "ready"; readonly copy: PublicFunnelCopy }
  | { readonly status: "error" };

export function usePublicFunnelCopy(): PublicFunnelCopyState {
  const [state, setState] = useState<PublicFunnelCopyState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    void loadPublicFunnelCopy()
      .then((copy) => {
        if (active) setState({ status: "ready", copy });
      })
      .catch(() => {
        if (active) setState({ status: "error" });
      });
    return () => {
      active = false;
    };
  }, []);

  return state;
}
