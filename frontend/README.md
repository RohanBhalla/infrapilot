# InfraPilot frontend

A React + TypeScript + Vite chat interface with Cognito hosted sign-in and a session-based API.
The current AWS deployment is a barebones integration test: the Lambda persists sessions in
DynamoDB and returns a fixed reply. The InfraPilot assistant, embeddings, RAG, and LLM are not
connected yet.

## Deployed demo

- **CloudFront website:** <https://dzmh6iokkia76.cloudfront.net>
- **Sign-in:** Open the website and select its sign-in button. Cognito is currently configured
  for a test user; do not publish or commit test-user passwords or other credentials.

## Screenshots

Put screenshots in `frontend/docs/images/` and reference them from this README using paths
relative to the README, for example:

```md
![Cognito authentication page](docs/images/authentication-page.png)
![CloudFront-hosted chat interface](docs/images/chat-interface.png)
```

## AWS components

| Component | Purpose |
|---|---|
| S3 | Stores the built HTML, JavaScript, CSS, and other static frontend assets. |
| CloudFront | Serves the S3 site through the public website domain. |
| Cognito | Authenticates users through its hosted sign-in experience. |
| API Gateway | Exposes the session API routes and validates Cognito JWTs. |
| Lambda | Implements the temporary session API and connects API Gateway to DynamoDB. |
| DynamoDB | Stores user-scoped session and message records. |

See [`../aws/lambda_chat/README.md`](../aws/lambda_chat/README.md) for the backend/API setup
and the test stub's limitations.

## Build and deploy the frontend

Build a React/Vite app with a chat page, message list, text input, and send button. This
repository already contains that frontend.

1. Configure the frontend environment variables as described below.
2. Build the app from this directory:

   ```bash
   npm install
   npm run build
   ```

3. Upload the **contents** of `dist/`—including `index.html` and the complete `assets/`
   directory—to the S3 bucket. Do not upload only `index.html`.
4. Configure the CloudFront distribution to use the S3 bucket as its origin, and set its
   default root object to `index.html`. The test distribution was created without WAF to avoid
   additional cost.
5. If the site still serves an older build, invalidate the CloudFront cache (for example, `/*`).

To run locally:

```bash
npm install
npm run dev
```

Open <http://localhost:5173>.

## Cognito and frontend configuration

Copy `.env.example` to `.env.local` and set the values for your AWS resources. `.env.local` is
intended for local configuration and is ignored by Git.

The Cognito app client must be a **public single-page application client with no client
secret**. Allow the exact callback and sign-out URLs in the app client's managed login settings:

- Local development: `http://localhost:5173/`
- Deployed site: `https://dzmh6iokkia76.cloudfront.net/`

Configure the user pool's hosted sign-in domain and the required OAuth scopes (including
`openid`). The frontend starts the Cognito sign-in redirect and handles the return to the
callback URL. The test user is managed in Cognito; do not place its credentials in this
repository.

Set `VITE_API_URL` to the API Gateway invoke URL once the API is deployed. Build-time `VITE_`
values are included in public browser assets. Never put a client secret, password, AWS access
key, or other private credential in a `VITE_` setting.

After changing `.env.local`, rebuild and upload the new `dist/` contents; changing the local
file alone does not update the deployed site.

## Backend connection

The frontend gets a Cognito access token and sends it as a bearer token with API requests. The
API Gateway HTTP API JWT authorizer should use:

- **Issuer:** `https://cognito-idp.<region>.amazonaws.com/<user-pool-id>`
- **Audience:** the public SPA app client ID (not a client with a secret)
- **Identity source:** `$request.header.Authorization`

Attach the authorizer to the session routes. Allow the exact CloudFront origin in API Gateway
CORS; allow `http://localhost:5173` as well if testing locally. Allow `GET`, `POST`, and
`OPTIONS`, and the `Authorization` and `Content-Type` headers.

The current frontend uses these routes:

- `POST /sessions` to create a session
- `GET /sessions/{session_id}` to restore it
- `POST /sessions/{session_id}/messages` to send a message

The Lambda test implementation stores sessions/messages in DynamoDB and returns a fixed reply.
It is a placeholder, not the production assistant.

## Errors encountered and resolutions

| Symptom | Resolution |
|---|---|
| CloudFront domain did not open the app | Set the CloudFront default root object to `index.html`. |
| Cognito login showed username/password but not email | Configure the appropriate user-pool sign-in options and hosted-login scopes (for example, `openid`, `email`, and `profile` as needed). |
| Cognito configuration was missing in the frontend | Set the `VITE_COGNITO_*` values in `.env.local`, rebuild, and upload the complete new `dist/` output. |
| Blank page or JavaScript error after login | Inspect the browser console and requested asset URLs. Verify the uploaded `index.html` references files present in S3, then invalidate CloudFront if it is serving stale HTML/assets. |
| Chat showed “The chat API is not configured yet” | Set `VITE_API_URL` to the API Gateway invoke URL and rebuild/redeploy the frontend. |
| Message API returned HTTP 500 | Check Lambda CloudWatch logs and environment variables. A leading space in `TABLE_NAME` caused DynamoDB's table-name validation to fail; use the exact table name with no surrounding whitespace. |

## Things to explore next

- Enable Cognito self-registration if appropriate.
- Customize the Cognito hosted-login branding.
- Replace the fixed Lambda response with the actual assistant/API integration.
- Improve error handling and add operational monitoring before broader use.
