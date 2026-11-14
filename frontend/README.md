# Chiefvoice Console

React operations console for the Chiefvoice API. It uses Material UI, Axios, TanStack React Query, and React Hook Form.

## Run locally

To run the full local stack (Postgres, API, development OIDC issuer, and console), from the repository root:

```sh
cp .env.docker.example .env.docker
docker compose up --build
```

Open <http://localhost:5173>. For local platform-admin access, open <http://localhost:9000/token>, copy the `access_token` value, and use **Connect API** in the console. This mock identity and token are for local development only; the issuer creates a fresh signing key whenever its container restarts, so fetch a new token after a restart. Live calls need real telephony and voice model credentials, which this local stack does not configure.

For separate development servers, start the API from the repository root using the instructions in the main README, then:

```sh
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/v1`, `/healthz`, and `/readyz` to the API at `http://127.0.0.1:8000`.

Select **Connect API** and paste an OIDC access token (recommended for setup) or a tenant API key. The token is stored in this browser's local storage. The frontend does not issue tokens or provision identity-provider users. The token needs endpoint scopes such as `accounts:read`, `agents:read`, `calls:read`, `integrations:read`, and `billing:read`; write actions need their corresponding `:write` scopes. Configuring a billing plan specifically requires a trusted OIDC token with `billing:admin`.

The **Customers** section is the platform operator view. It requires an OIDC principal whose subject is in `PLATFORM_ADMIN_SUBJECTS`, with `platform:customers:read` and `platform:customers:write` scopes. It can provision isolated customer accounts, issue one-time API keys, and rotate or revoke keys. Customer API keys are scoped credentials for a customer account; they are not separate accounts themselves. In local Compose, the mock token is a development-only platform admin for `local-demo-tenant`.

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
