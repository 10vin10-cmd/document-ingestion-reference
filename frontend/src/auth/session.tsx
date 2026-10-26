import { createContext, useContext } from "react";

export const REVIEWER_GROUP = "reviewers";

export interface Session {
  /** Matches the username the API records as claimed_by / reviewed_by. */
  username: string;
  displayName: string;
  signOut(): void;
}

const SessionContext = createContext<Session | null>(null);

export const SessionProvider = SessionContext.Provider;

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession must be used inside <SessionProvider>");
  return session;
}
