import React from "react";
import ReactDOM from "react-dom/client";
import {
  BrowserCacheLocation,
  InteractionRequiredAuthError,
  PublicClientApplication,
} from "@azure/msal-browser";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";

import { ConsoleApiClient } from "./api/client";
import { Claim360Page } from "./pages/Claim360Page";
import { ReviewQueuePage } from "./pages/ReviewQueuePage";
import "./styles.css";

function required(name: keyof ImportMetaEnv): string {
  const value = import.meta.env[name];
  if (typeof value !== "string" || !value.trim()) throw new Error(`${name} is required`);
  return value.trim();
}

async function bootstrap() {
  const tenantId = required("VITE_ENTRA_TENANT_ID");
  const authority = required("VITE_ENTRA_AUTHORITY");
  const expectedAuthority = `https://login.microsoftonline.com/${tenantId}/v2.0`;
  if (authority.replace(/\/$/, "") !== expectedAuthority) {
    throw new Error("VITE_ENTRA_AUTHORITY must be the tenant-specific v2 authority");
  }
  required("VITE_ENTRA_API_AUDIENCE");
  const scope = required("VITE_ENTRA_API_SCOPE");
  const msal = new PublicClientApplication({
    auth: {
      clientId: required("VITE_ENTRA_CLIENT_ID"),
      authority: expectedAuthority,
      redirectUri: required("VITE_ENTRA_REDIRECT_URI"),
    },
    cache: {
      cacheLocation: BrowserCacheLocation.SessionStorage,
      storeAuthStateInCookie: false,
    },
  });
  await msal.initialize();
  const redirect = await msal.handleRedirectPromise();
  if (redirect?.account) msal.setActiveAccount(redirect.account);
  const account = msal.getActiveAccount() ?? msal.getAllAccounts()[0];
  if (!account) {
    await msal.loginRedirect({ scopes: [scope] });
    return;
  }
  msal.setActiveAccount(account);
  const api = new ConsoleApiClient({
    baseUrl: required("VITE_API_BASE_URL"),
    getAccessToken: async () => {
      try {
        return (await msal.acquireTokenSilent({ account, scopes: [scope] })).accessToken;
      } catch (error) {
        if (error instanceof InteractionRequiredAuthError) {
          await msal.acquireTokenRedirect({ account, scopes: [scope] });
        }
        throw error;
      }
    },
  });

  function ClaimRoute() {
    const { claimId = "" } = useParams();
    return <Claim360Page api={api} claimId={claimId} />;
  }

  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <QueryClientProvider client={new QueryClient()}>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Navigate to="/queue" replace />} />
            <Route path="/queue" element={<ReviewQueuePage api={api} />} />
            <Route path="/claims/:claimId" element={<ClaimRoute />} />
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </React.StrictMode>,
  );
}

void bootstrap();
