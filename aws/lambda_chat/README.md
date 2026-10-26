# Authenticated chat API (AWS Console)

> These console steps are kept as a record of the first setup. The same resources are now
> defined in CDK: deploy with [`infra/`](../../infra/README.md) instead of creating them by hand.

This Lambda is a temporary API integration test. It stores sessions and messages in DynamoDB
and returns a fixed reply. It does not call InfraPilot's assistant, embeddings, RAG, or an LLM.

## 1. Create the DynamoDB table

In the DynamoDB console, create a table with:

- Partition key: `PK` (String)
- Sort key: `SK` (String)
- Capacity: on-demand, if appropriate for the shared account

The Lambda stores each user's sessions in that user's partition. Session metadata and messages
are separate items. The user ID comes from Cognito's verified `sub` claim.

## 2. Create the Lambda

In the Lambda console:

1. Create a function from scratch using Python 3.12 or a currently supported Python runtime.
2. Create or select an execution role with basic Lambda CloudWatch Logs permissions and
   `dynamodb:PutItem` and `dynamodb:Query` on only the test sessions table.
3. In **Configuration → Environment variables**, add `TABLE_NAME` with the exact table name.
4. In **Code**, upload `handler.py` from this folder (or paste its contents into `lambda_function.py`
   and set the handler to `lambda_function.lambda_handler`).
5. Set the handler setting to `handler.lambda_handler` when the source file is named `handler.py`.
6. Deploy the function.

The standard Python Lambda runtime includes boto3. No package install is needed for this file.

## 3. Create the API Gateway HTTP API

In API Gateway, create an **HTTP API**, add a Lambda integration for this function, and configure
these routes (HTTP API payload format 2.0):

| Method | Route |
|---|---|
| `POST` | `/sessions` |
| `GET` | `/sessions/{session_id}` |
| `POST` | `/sessions/{session_id}/messages` |
| `GET` | `/health` (optional, can be unauthenticated) |

Attach the Cognito JWT authorizer to all session routes. Configure the issuer as
`https://cognito-idp.<region>.amazonaws.com/<user-pool-id>` and the audience as the **public SPA
app client ID**. Use the API's `$default` stage for the simplest invoke URL, and enable automatic
deployment while testing.

Enable API CORS for the exact CloudFront website origin and, during local development,
`http://localhost:5173`. Allow `Authorization` and `Content-Type` headers and `GET`, `POST`, and
`OPTIONS` methods. API Gateway handles preflight requests from the browser.

## 4. Build the frontend configuration

Copy `frontend/.env.example` to `frontend/.env.local`, then fill in the real Cognito values and
API Gateway invoke URL. The Cognito domain value is its hostname, without `https://`. Use the
CloudFront URL as the redirect and sign-out URL. Those exact URLs must also be allowed in the
Cognito app client's managed-login settings.

From the `frontend/` directory:

```bash
npm install
npm run build
```

In the S3 console, upload the **contents** of `frontend/dist/` to the same location as before,
including `index.html` and the complete `assets/` folder. Invalidate the CloudFront distribution
if it continues serving the previous build.

## 5. End-to-end check

1. Open the CloudFront website and sign in with the Cognito test user.
2. Send a message. The browser sends the Cognito access token to API Gateway.
3. API Gateway validates the JWT and passes the verified `sub` claim to Lambda.
4. Lambda stores the session and both messages in DynamoDB and returns the fixed test response.
5. Reload the site and check that the session history is restored.
6. Try another Cognito user and verify they cannot read the first user's session (the Lambda
   scopes every lookup by the authenticated user's `sub`).

## Limitations

- This is a test stub, not production-ready chat. The fixed answer explicitly says RAG is not
  connected.
- It is designed for API Gateway HTTP API JWT-authorizer events, not direct Lambda Function URL
  events.
- The frontend stores the current session ID in browser session storage.
- Add throttling, logging/retention, and operational monitoring before broader use.
