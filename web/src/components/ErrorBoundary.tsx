import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("VideoScope dashboard error", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <main className="fatal-error">
          <p className="eyebrow">Interface error</p>
          <h1>The dashboard could not be rendered.</h1>
          <p>{this.state.error.message}</p>
          <button type="button" onClick={() => window.location.reload()}>
            Reload dashboard
          </button>
        </main>
      );
    }
    return this.props.children;
  }
}
