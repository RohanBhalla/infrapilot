import { Amplify } from "aws-amplify";
import {
  fetchAuthSession,
  getCurrentUser,
  signInWithRedirect,
  signOut,
} from "aws-amplify/auth";

const region = import.meta.env.VITE_COGNITO_REGION;
const userPoolId = import.meta.env.VITE_COGNITO_USER_POOL_ID;
const userPoolClientId = import.meta.env.VITE_COGNITO_CLIENT_ID;
const domain = import.meta.env.VITE_COGNITO_DOMAIN?.replace(/^https?:\/\//, "").replace(/\/$/, "");
const redirectUrl = import.meta.env.VITE_COGNITO_REDIRECT_URL || window.location.origin;
const signOutUrl = import.meta.env.VITE_COGNITO_SIGN_OUT_URL || redirectUrl;

export const authConfigured = Boolean(region && userPoolId && userPoolClientId && domain);

if (authConfigured) {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: userPoolId!,
        userPoolClientId: userPoolClientId!,
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
