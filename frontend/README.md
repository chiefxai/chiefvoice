# Chiefvoice Console

Platform console for the Chiefvoice API. It has two focused views: a cross-customer phone number inventory across providers, and API keys assigned to one outbound-enabled number. It uses Material UI, Axios, and TanStack React Query.

## Run locally

To run the full local stack (Postgres, API, development OIDC issuer, and console), from the repository root:

```sh
cp .env.docker.example .env.docker
docker compose up --build
```

Open <http://localhost:5173> and select **Sign in with OIDC**. The local identity provider displays its own sign-in page; select **Continue as local admin** to enter the console. The browser uses the authorization-code flow with PKCE, and the access token is kept in session storage. Its test signing key persists in a local Docker volume across provider restarts. The local identity provider and its administrator are for development only. Live calls need real telephony and voice model credentials, which this local stack does not configure.

Local Compose stores encrypted recordings in MinIO. Open <http://localhost:9003> for the MinIO console (`chiefvoice-local` / `chiefvoice-local-storage-password`); the S3 API is at <http://localhost:9002>.

For separate development servers, start the API from the repository root using the instructions in the main README, then:

```sh
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/v1`, `/healthz`, and `/readyz` to the API at `http://127.0.0.1:8000`.

The console signs administrators in through OIDC. For a real identity provider, set `VITE_OIDC_AUTHORITY` to its issuer URL, `VITE_OIDC_CLIENT_ID` to a public SPA client registered for the console origin, and optionally `VITE_OIDC_REDIRECT_URI` and `VITE_OIDC_SCOPE`. Register the redirect URI (by default, `<console-origin>/`) as an allowed callback URL. The provider must issue the API access token with the configured backend audience, a `tenant_id` claim, and `platform:customers:read` / `platform:customers:write` scopes. Configure the backend's `OIDC_ISSUER`, `OIDC_AUDIENCE`, and `OIDC_JWKS_URL` for the same provider. The frontend never handles a client secret.

The console requires an OIDC principal whose subject is in `PLATFORM_ADMIN_SUBJECTS`, with `platform:customers:read` and `platform:customers:write` scopes. The Numbers view lists registered customer numbers across providers. The API view issues one-time keys assigned to an outbound-enabled number, and can revoke them. Each issued key is limited to calls and call results for its assigned number. In local Compose, the mock identity signs in as a development-only platform admin for `local-demo-tenant`.

## Production API URL

Set `VITE_API_URL` to the API origin when building for a separately hosted frontend, for example:

```sh
VITE_API_URL=https://api.example.com npm run build
```

The API must allow the deployed frontend origin through CORS when hosted on a different origin. The current Vite proxy is development-only.

## Current API boundaries

- Call history is local to this browser because the API currently provides lookup by session ID, not a tenant call-list endpoint.
- API readiness reflects backend configuration; the frontend cannot configure cloud credentials, public callback URLs, identity-provider settings, or provider webhook secrets.
- Supported integration management is currently wired for Vobiz. Other provider adapters are not implemented yet.
