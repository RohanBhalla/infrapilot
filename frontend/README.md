# InfraPilot frontend

A React + TypeScript + Vite chat UI with Cognito hosted sign-in and the session-based API.

## Run locally

From this directory:

```bash
npm install
npm run dev
```

Copy `.env.example` to `.env.local`, enter the Cognito public app settings, and use
`http://localhost:5173/` as the redirect and sign-out URL for local development. The Cognito
app client must allow that URL. Open `http://localhost:5173`. After sign-in, the app creates a
chat session, keeps its ID in browser session storage, restores the conversation when possible,
and sends messages to `POST /sessions/{id}/messages`.

For the deployed site, set the redirect/sign-out URLs to the CloudFront URL. Set `VITE_API_URL`
to the API Gateway invoke URL when that API is deployed. Build-time values are public browser
configuration; never put AWS credentials or client secrets in `VITE_` settings.

The app obtains a Cognito access token and sends it as a bearer token with each API request.
If the API URL is not configured, sign-in still works, but chat requests show that the API is
not configured.

## Build

```bash
npm run build
npm run preview
```

The deployable site is the **contents** of `dist/`, not the `frontend/` source folder or only
`dist/index.html`. Upload the complete build output—including `index.html` and `assets/`—to
the S3 bucket using the AWS Console. Configure CloudFront's default root object as `index.html`.
After uploading an update, invalidate the CloudFront cache in the console if it still serves
old files.
