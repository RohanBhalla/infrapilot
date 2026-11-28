# InfraPilot to-do list

What's left to build, in the order we plan to do it. Update this file in the same PR as the
work: tick the box and add the PR number. Details for each item are in the
[development plan](DEVELOPMENT_PLAN.md) and the [decisions](decisions/).

Last updated: 2026-11-27

## Now: users' own sites ([decision 006](decisions/006-user-code.md))

- [x] Decision 006: accept a public repo link (read for context) and a static site zip, never run users' code (#20)
- [x] Read a linked public GitHub repo to fill in the chat's questions (`assistant/infrapilot_assistant/repo.py`) (#20)
- [x] Deploy the repo reading to AWS and try it with a real repo (2026-11-24: a React repo filled in `build`, a Python repo the language and version)
- [x] Static site zip upload (#21, deployed 2026-11-27)
  - [x] `POST /requests/{id}/upload`: a short-lived S3 upload link (`uploads/<request>/site.zip`, deleted after 1 day)
  - [x] Checks before deploy: ≤ 20 MB zipped, ≤ 50 MB unzipped, ≤ 1,000 files, web file types only, an `index.html`, no `..` or absolute paths, no links
  - [x] The sandbox ops Lambda copies the checked files into the site bucket instead of the sample page
  - [x] Upload button on the deploy card (before Approve), with the reason shown when a zip is refused
  - [x] Tests
  - [x] AWS run through the API: bad zip refused (names `upload.php`), good zip deployed, HTML/CSS/JS served with the right types, torn down cleanly
  - [x] Browser run on the live site (2026-11-27): bad zip refused on the card, good zip deployed and showed the page with its CSS and JS, torn down from the card

## Waiting on AWS deploy

- [x] Deploy PR #19 (keep the file checker warm): a new plan's checks show "passed" (2026-11-24). Right after a deploy the checker needs ~45 s to warm up

## Not yet tried on AWS

- [x] Sign-in on the live site broke (localhost redirect baked into the build); fixed and deployed in #23

- [x] Lambda sandbox with a function URL through the API (2026-11-27): deployed, URL returned the placeholder, torn down
- [ ] Lambda sandbox with the other triggers (API Gateway, schedule, S3) from the website
- [x] Dockerfile + ECR-only plan in the sandbox (2026-11-27): repository created and deleted
- [x] Keep every checker part warm: the ping only loaded the CloudFormation checks, so a plan with a Dockerfile took ~15 s and its checks showed "skipped" (fixed in #22, deployed: the same container plan now shows all five checks passed)
- [ ] Container plans always include a GitHub Actions workflow, even when the user says no; add a `ci_deploy` choice like the other generators

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
