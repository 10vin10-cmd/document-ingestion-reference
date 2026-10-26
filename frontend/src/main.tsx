import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ApiProvider } from "./api/context";
import { SessionProvider } from "./auth/session";
import { loadConfig } from "./config";
import "./styles.css";

const DEMO = import.meta.env.VITE_DEMO === "true";
const root = createRoot(document.getElementById("root")!);
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });

const render = (node: ReactNode) =>
  root.render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>{node}</QueryClientProvider>
    </StrictMode>,
  );

async function start() {
  if (DEMO) {
    // Loaded only in demo builds; the production bundle never contains it.
    const { createDemoApi, DEMO_USER } = await import("./api/demo");
    const session = { username: DEMO_USER, displayName: "demo.reviewer@example.com", signOut: () => window.location.reload() };
    render(
      <SessionProvider value={session}>
        <ApiProvider value={createDemoApi()}>
          <App demo />
        </ApiProvider>
      </SessionProvider>,
    );
    return;
  }

  try {
    const config = await loadConfig();
    const { CognitoGate } = await import("./auth/CognitoGate");
    render(
      <CognitoGate config={config}>
        <App />
      </CognitoGate>,
    );
  } catch (err) {
    render(
      <main className="full-page">
        <h1>Invoice Review</h1>
        <p className="alert alert-error">{(err as Error).message}</p>
      </main>,
    );
  }
}

void start();
