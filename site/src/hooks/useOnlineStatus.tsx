import {
  createContext,
  type PropsWithChildren,
  useContext,
  useSyncExternalStore,
} from "react";

const OnlineStatusContext = createContext<boolean | undefined>(undefined);

function subscribe(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

function onlineSnapshot() {
  return navigator.onLine;
}

export function OnlineStatusProvider({
  children,
  online,
}: PropsWithChildren<{ online?: boolean }>) {
  return (
    <OnlineStatusContext.Provider value={online}>
      {children}
    </OnlineStatusContext.Provider>
  );
}

export function useOnlineStatus() {
  const override = useContext(OnlineStatusContext);
  const browserStatus = useSyncExternalStore(subscribe, onlineSnapshot, () => true);
  return override ?? browserStatus;
}
