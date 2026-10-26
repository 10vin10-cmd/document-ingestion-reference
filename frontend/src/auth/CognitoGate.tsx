import { WebStorageStateStore } from "oidc-client-ts";
import { useMemo, useRef, type ReactNode } from "react";
import { AuthProvider, useAuth } from "react-oidc-context";
import { ApiProvider } from "../api/context";
import { createHttpApi } from "../api/client";
import type { AppConfig } from "../config";
import { REVIEWER_GROUP, SessionProvider, type Session } from "./session";

/**
 * Sign-in through the Cognito hosted UI using the authorization code flow
 * with PKCE (no client secret in the browser). Tokens are kept in
 * sessionStorage, so closing the tab ends the session.
 */
export function CognitoGate({ config, children }: { config: AppConfig; children: ReactNode }) {
  const appUrl = new URL(".", window.location.href).href;
  return (
    <AuthProvider
      authority={`https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`}
      client_id={config.userPoolClientId}
      redirect_uri={appUrl}
      response_type="code"
      scope="openid email profile"
      automaticSilentRenew
      userStore={new WebStorageStateStore({ store: window.sessionStorage })}
      onSigninCallback={() => {
        // Drop ?code=&state= from the address bar after sign-in.
        window.history.replaceState({}, document.title, window.location.pathname);
      }}
    >
      <SessionBridge config={config} appUrl={appUrl}>
        {children}
      </SessionBridge>
    </AuthProvider>
  );
}

function SessionBridge({ config, appUrl, children }: { config: AppConfig; appUrl: string; children: ReactNode }) {
  const auth = useAuth();
  const tokenRef = useRef<string | undefined>(undefined);
  tokenRef.current = auth.user?.id_token;

  const api = useMemo(() => createHttpApi(config.apiBaseUrl, () => tokenRef.current), [config.apiBaseUrl]);

  const session = useMemo<Session | null>(() => {
    const profile = auth.user?.profile;
    if (!profile) return null;
    const username = String(profile["cognito:username"] ?? profile.sub);
    return {
      username,
      displayName: String(profile.email ?? username),
      signOut: () => {
        void auth.removeUser().then(() => {
          // Cognito does not publish an end_session endpoint; use its logout URL.
          const logout = new URL("/logout", config.authDomain);
          logout.searchParams.set("client_id", config.userPoolClientId);
          logout.searchParams.set("logout_uri", appUrl);
          window.location.assign(logout.href);
        });
      },
    };
  }, [auth, config, appUrl]);

  if (auth.isLoading) return <FullPage>Signing you in…</FullPage>;
  if (auth.error) {
    return (
      <FullPage>
        <p>Sign-in failed: {auth.error.message}</p>
        <button className="btn btn-primary" onClick={() => void auth.signinRedirect()}>Try again</button>
      </FullPage>
    );
  }
  if (!auth.isAuthenticated || !session) {
    return (
      <FullPage>
        <h1>Invoice Review</h1>
        <p>Review documents the extraction pipeline could not verify on its own.</p>
        <button className="btn btn-primary" onClick={() => void auth.signinRedirect()}>Sign in</button>
      </FullPage>
    );
  }

  const groups = auth.user?.profile["cognito:groups"];
  if (!Array.isArray(groups) || !groups.includes(REVIEWER_GROUP)) {
    return (
      <FullPage>
        <p>
          You are signed in as <strong>{session.displayName}</strong>, but your account is not in the{" "}
          <code>{REVIEWER_GROUP}</code> group. Ask an administrator for access.
        </p>
        <button className="btn" onClick={session.signOut}>Sign out</button>
      </FullPage>
    );
  }

  return (
    <SessionProvider value={session}>
      <ApiProvider value={api}>{children}</ApiProvider>
    </SessionProvider>
  );
}

function FullPage({ children }: { children: ReactNode }) {
  return <main className="full-page">{children}</main>;
}
