/**
 * Runtime configuration, written next to the app by the CDK deployment. One
 * build serves every environment; nothing environment-specific is baked into
 * the bundle.
 */
export interface AppConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  /** Cognito hosted sign-in domain, e.g. https://x.auth.ap-south-1.amazoncognito.com */
  authDomain: string;
  /** Empty when the API is served from the app's own origin. */
  apiBaseUrl: string;
}

const REQUIRED: (keyof AppConfig)[] = ["region", "userPoolId", "userPoolClientId", "authDomain"];

export async function loadConfig(): Promise<AppConfig> {
  const res = await fetch(new URL("config.json", document.baseURI), { cache: "no-store" });
  if (!res.ok) {
    throw new Error("config.json is missing. Copy public/config.example.json to public/config.json, or run `npm run dev:demo`.");
  }
  const config = (await res.json()) as Partial<AppConfig>;
  const missing = REQUIRED.filter((key) => !config[key]);
  if (missing.length) throw new Error(`config.json is missing: ${missing.join(", ")}`);
  return { apiBaseUrl: "", ...config } as AppConfig;
}
