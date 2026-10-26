// Settings come from /config.json, which `cdk deploy` writes next to the site (see infra/).
// The VITE_ variables are the fallback for local development, where there is no config.json.
export type AppConfig = {
  region?: string;
  userPoolId?: string;
  clientId?: string;
  domain?: string;
  redirectUrl?: string;
  signOutUrl?: string;
  apiUrl?: string;
};

const env = import.meta.env;

export const config: AppConfig = {
  region: env.VITE_COGNITO_REGION,
  userPoolId: env.VITE_COGNITO_USER_POOL_ID,
  clientId: env.VITE_COGNITO_CLIENT_ID,
  domain: env.VITE_COGNITO_DOMAIN,
  redirectUrl: env.VITE_COGNITO_REDIRECT_URL,
  signOutUrl: env.VITE_COGNITO_SIGN_OUT_URL,
  apiUrl: env.VITE_API_URL ?? (env.DEV ? "http://127.0.0.1:8000" : undefined),
};

export async function loadConfig(): Promise<void> {
  try {
    const response = await fetch("/config.json", { cache: "no-store" });
    // The Vite dev server answers unknown paths with index.html, so check the type too.
    if (!response.ok || !response.headers.get("content-type")?.includes("json")) return;
    Object.assign(config, (await response.json()) as AppConfig);
  } catch {
    // No deployed config: keep the VITE_ settings.
  }
}
