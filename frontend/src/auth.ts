import { Amplify } from "aws-amplify";
import {
  fetchAuthSession,
  getCurrentUser,
  signInWithRedirect,
  signOut,
} from "aws-amplify/auth";

import { config } from "./config";

export let authConfigured = false;

// Call once, after loadConfig().
export function configureAuth(): void {
  const { region, userPoolId, clientId } = config;
  const domain = config.domain?.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const redirectUrl = config.redirectUrl || window.location.origin;
  const signOutUrl = config.signOutUrl || redirectUrl;

  authConfigured = Boolean(region && userPoolId && clientId && domain);
  if (!authConfigured) return;
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: userPoolId!,
        userPoolClientId: clientId!,
        loginWith: {
          oauth: {
            domain: domain!,
            scopes: ["openid"],
            redirectSignIn: [redirectUrl],
            redirectSignOut: [signOutUrl],
            responseType: "code",
          },
        },
      },
    },
  });
}

export async function currentUser() {
  return getCurrentUser();
}

export async function accessToken(): Promise<string> {
  const session = await fetchAuthSession();
  const token = session.tokens?.accessToken;
  if (!token) {
    throw new Error("Your sign-in has expired. Sign in again to continue.");
  }
  return token.toString();
}

export function beginSignIn(): Promise<void> {
  return signInWithRedirect();
}

export function beginSignOut(): Promise<void> {
  return signOut();
}
