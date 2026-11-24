# 006: What we accept from users' own code

- **Status:** accepted
- **Date:** 2026-11-24
- **Changes:** [decision 004](004-sandbox-deploys.md), point 2 ("sandboxes run the placeholder code")

## Decision

We accept two things from users, and nothing that runs in our account:

1. **A link to a public GitHub repo, read for context.** When a message links
   `github.com/<owner>/<repo>`, the assistant lists the repo's top level (one GitHub API call)
   and reads a few small files (package.json, angular.json, pyproject.toml, go.mod, Dockerfile,
   .nvmrc, .python-version). Plain rules turn them into answers the chat would otherwise ask
   for: language, package manager and version, framework, the build folder, and the port. The
   user's own words always win over what we found.
   - Nothing from the repo runs, and nothing from it is sent to the model, so a repo can't
     carry instructions to the assistant. Only the picked-out values are used.
   - Public repos only (private repos need a GitHub sign-in). Each file is capped at 200 KB and
     each request at 4 s. If GitHub is slow or limits us, the chat asks the questions as before.
   - Each repo is read once per chat session.
2. **A built static site, uploaded as a zip** (next step, not built yet). The deploy card gets
   an upload button. The zip goes to S3 through a short-lived upload link and is checked
   (size, file count, web file types only, an `index.html`, no paths outside the folder) before
   the sandbox copies it into the site bucket instead of the sample page. HTML, CSS and
   JavaScript run in the visitor's browser, not in our account.

We **don't** run users' code: no uploaded Lambda code and no building from a repo
(CodeBuild). If we ever add them, they are team-only, like ECS and EKS.

## Why

- Repo context fixes the clumsy questions ("which folder has the built site?") with almost no
  risk or cost.
- An uploaded static site turns "it deploys a sample page" into "it deploys your site", and
  static files can't act inside our AWS account.
- Uploaded Lambda code or a repo's build scripts would run inside the team account. The
  permissions boundary limits what they can reach, but they could still spend money or send
  traffic from our IPs, and with the account's Lambda limit of 10 we can't cap how many copies
  run.

## Cost

Repo reading: about $0 (a few HTTP requests per linked repo; GitHub allows 60 listings an hour
without a token, and an optional `GITHUB_TOKEN` raises that). Zip uploads: S3 storage for a day
per upload, about $0.
