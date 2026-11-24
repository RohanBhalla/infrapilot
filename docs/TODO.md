# InfraPilot to-do list

What's left to build, in the order we plan to do it. Update this file in the same PR as the
work: tick the box and add the PR number. Details for each item are in the
[development plan](DEVELOPMENT_PLAN.md) and the [decisions](decisions/).

Last updated: 2026-11-24

## Now: users' own sites ([decision 006](decisions/006-user-code.md))

- [x] Decision 006: accept a public repo link (read for context) and a static site zip, never run users' code
- [x] Read a linked public GitHub repo to fill in the chat's questions (`assistant/infrapilot_assistant/repo.py`)
- [ ] Deploy the repo reading to AWS and try it from the website with a real repo
- [ ] Static site zip upload
  - [ ] `POST /requests/{id}/upload`: a short-lived S3 upload link (`uploads/<user>/<request>.zip`, deleted after 1 day)
  - [ ] Checks before deploy: ≤ 20 MB zipped, ≤ 50 MB unzipped, ≤ 1,000 files, web file types only, an `index.html`, no `..` or absolute paths, no links
  - [ ] The sandbox ops Lambda copies the checked files into the site bucket instead of the sample page
  - [ ] Upload button on the deploy card (before Approve), with the reason shown when a zip is refused
  - [ ] Tests, then an AWS run: upload a real `dist/` and open the site

## Waiting on AWS deploy

- [ ] Deploy PR #19 (keep the file checker warm) and check a new plan's checks show "passed", not "skipped"

## Not yet tried on AWS

- [ ] Lambda sandbox from the website (function URL, API Gateway, schedule, S3 trigger), allowed but only tested by hand in step 2
- [ ] Dockerfile + ECR-only plan in the sandbox

## Later: deploy plan (Features 4.2 and 4.3)

- [ ] Step 7.3: GitHub Actions target, commit or PR to a team sample repo (GitHub token in SSM)
- [ ] Step 7.4: Container on ECS, with CodeBuild building the sample image. **Paid while running (~$0.015/hour): ask first.** Team-only
- [ ] Step 7.5: EKS, after its generator exists; scheduled test windows only. **Paid: ask first.** Team-only
- [ ] Step 8: tear down from the chat ("delete my site")
- [ ] Feature 4.3: `explain_failure` eval (≥ 25 real failure logs) and the EKS generator

## Later: hardening and demo (Phases 5 and 6)

- [ ] Phase 5: end-to-end evaluation and hardening
- [ ] Phase 6: stretch goals and the demo

## Not doing (decision 006)

- Uploading Lambda code, or building from a repo with CodeBuild: users' code would run in the team account. Team-only if ever added.
- Reading private repos (needs a GitHub sign-in).
