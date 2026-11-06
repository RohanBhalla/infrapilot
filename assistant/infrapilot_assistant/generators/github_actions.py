"""GitHub Actions CI/CD workflow generator (plan C10, the first generator).

Deterministic: the workflow is assembled from tested building blocks for each language and
deploy target, so the same details always give the same file and there's no model call. Action
versions come from the version registry. Free-text details (steps, triggers) are read with
simple keyword rules; anything we couldn't use is listed in the plan's notes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..plan import Plan, PlanFile, Source
from . import pins, validate

PATH = ".github/workflows/ci.yml"
STEP_ORDER = ("format", "lint", "typecheck", "test", "build")
STEP_WORDS = {
    "format": "format", "formatting": "format", "prettier": "format",
    "lint": "lint", "linting": "lint", "linter": "lint", "eslint": "lint", "ruff": "lint",
    "typecheck": "typecheck", "type-check": "typecheck", "type check": "typecheck", "types": "typecheck",
    "tsc": "typecheck", "mypy": "typecheck",
    "test": "test", "tests": "test", "testing": "test", "unit tests": "test", "pytest": "test",
    "build": "build", "compile": "build", "bundle": "build", "package": "build",
}
DEFAULT_PM = {"node": "npm", "python": "pip", "java": "maven", "go": "go"}
PMS = {"node": ("npm", "yarn", "pnpm"), "python": ("pip", "poetry"), "java": ("maven", "gradle"), "go": ("go",)}
BRANCHES = ("main", "master", "develop", "dev", "staging")
# Deploy on pushes and manual runs of the deploy branch; never on PRs or scheduled runs.
DEPLOY_IF = "(github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == '{ref}'"

DOCS = [
    Source(title="Workflow syntax for GitHub Actions",
           url="https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax"),
]
OIDC_DOCS = [
    Source(title="Configuring OpenID Connect in Amazon Web Services",
           url="https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws"),
    Source(title="Create an OpenID Connect (OIDC) identity provider in IAM",
           url="https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html"),
]
ECS_DOCS = [
    Source(title="Deploying to Amazon Elastic Container Service",
           url="https://docs.github.com/en/actions/how-tos/deploy/deploy-to-third-party-platforms/"
               "amazon-elastic-container-service"),
]


# ---------------------------------------------------------------- YAML rendering

SAFE = re.compile(r"^[\w${}][\w ./@${}|&!=()'*-]*$")


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    # Plain scalars YAML would read as something else (numbers, booleans) or that hold ": " get quoted.
    if SAFE.match(s) and ": " not in s and not re.fullmatch(r"[\d.]+|true|false|yes|no|on|off|null", s, re.I):
        return s
    return json.dumps(s)


def step(name: str, *, uses: str | None = None, run: str | None = None, with_: dict | None = None,
         id: str | None = None, if_: str | None = None, env: dict | None = None) -> str:
    lines = [f"- name: {_scalar(name)}"]
    if id:
        lines.append(f"  id: {id}")
    if if_:
        lines.append(f"  if: {if_}")
    if uses:
        lines.append(f"  uses: {uses}")
    if with_:
        lines.append("  with:")
        lines += [f"    {k}: {_scalar(v)}" for k, v in with_.items()]
    if env:
        lines.append("  env:")
        lines += [f"    {k}: {_scalar(v)}" for k, v in env.items()]
    if run is not None:
        if "\n" in run or _scalar(run) != run:  # block style keeps shell quoting readable
            lines.append("  run: |")
            lines += [f"    {r}" if r else "" for r in run.splitlines()]
        else:
            lines.append(f"  run: {_scalar(run)}")
    return "\n".join(lines)


def _indent(block: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in block.splitlines())


# ---------------------------------------------------------------- reading the details

def parse_steps(text: str | None) -> tuple[list[str], list[str]]:
    """'lint, unit tests and build' -> ['lint', 'test', 'build'], plus words we didn't use."""
    if not text:
        return ["lint", "test", "build"], []
    found, unknown = set(), []
    for part in re.split(r",|;|\band\b|\bthen\b|&|\+|/", text.lower()):
        word = part.strip(" .")
        if not word:
            continue
        match = STEP_WORDS.get(word) or next((v for k, v in STEP_WORDS.items()
                                              if re.search(rf"\b{re.escape(k)}\b", word)), None)
        if match:
            found.add(match)
        else:
            unknown.append(word)
    steps = [s for s in STEP_ORDER if s in found]
    return (steps or ["lint", "test", "build"]), unknown


@dataclass
class Triggers:
    push: bool = True
    pull_request: bool = True
    schedule: bool = False
    tags: bool = False
    branches: list[str] = field(default_factory=lambda: ["main"])

    def render(self) -> str:
        branches = "[" + ", ".join(self.branches) + "]"
        lines = ["on:"]
        if self.push or self.tags:
            lines.append("  push:")
            if self.push:
                lines.append(f"    branches: {branches}")
            if self.tags:
                lines.append('    tags: ["v*"]')
        if self.pull_request:
            lines += ["  pull_request:", f"    branches: {branches}"]
        if self.schedule:
            lines += ["  schedule:", '    - cron: "0 6 * * *"  # every day at 06:00 UTC']
        lines.append("  workflow_dispatch:  # also lets you run it by hand from the Actions tab")
        return "\n".join(lines)


def parse_triggers(text: str | None) -> Triggers:
    t = (text or "").lower()
    branches = [b for b in BRANCHES if re.search(rf"\b{b}\b", t)] or ["main"]
    pr = bool(re.search(r"pull[ _-]?requests?|\bprs?\b", t))
    push = bool(re.search(r"\bpush|commit|merge", t))
    schedule = bool(re.search(r"nightly|daily|weekly|schedule|cron|every (day|night)", t))
    tags = bool(re.search(r"\btags?\b|releases?\b", t))
    if not (pr or push or schedule or tags):
        push = pr = True  # nothing we recognise: the usual default
    return Triggers(push=push, pull_request=pr, schedule=schedule, tags=tags, branches=branches)


def _major_version(text, fallback: str | None) -> str | None:
    m = re.search(r"\d+(?:\.\d+)?", str(text or ""))
    return m.group() if m else fallback


# ---------------------------------------------------------------- languages

@dataclass
class Toolchain:
    setup: list[str]                  # rendered steps
    commands: dict[str, str]          # step -> shell command
    notes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


def _node(pm: str, version: str | None, v: dict) -> Toolchain:
    setup, actions = [], ["actions/setup-node"]
    if pm == "pnpm":
        actions.insert(0, "pnpm/action-setup")
        setup.append(step("Set up pnpm", uses=pins.pin("pnpm/action-setup", v)))
    setup.append(step("Set up Node.js", uses=pins.pin("actions/setup-node", v),
                      with_={"node-version": version or "lts/*", "cache": pm}))
    install = {"npm": "npm ci", "yarn": "yarn install --frozen-lockfile",
               "pnpm": "pnpm install --frozen-lockfile"}[pm]
    setup.append(step("Install dependencies", run=install))
    script = {"npm": "npm run {} --if-present", "yarn": "yarn run {}", "pnpm": "pnpm run --if-present {}"}[pm]
    commands = {"format": script.format("format:check"), "lint": script.format("lint"),
                "typecheck": script.format("typecheck"), "test": script.format("test"),
                "build": script.format("build")}
    notes = [] if pm != "yarn" else ["Yarn has no `--if-present`, so every step needs a matching script in "
                                     "package.json."]
    if pm == "pnpm":
        notes.append("pnpm/action-setup reads the pnpm version from the `packageManager` field in package.json.")
    return Toolchain(setup, commands, notes, actions)


def _python(pm: str, version: str | None, v: dict) -> Toolchain:
    setup, notes = [], []
    if pm == "poetry":
        setup.append(step("Install Poetry", run="pipx install poetry"))
    setup.append(step("Set up Python", uses=pins.pin("actions/setup-python", v),
                      with_={"python-version": version or "3.x", "cache": pm}))
    if pm == "poetry":
        setup.append(step("Install dependencies", run="poetry install --no-interaction"))
        r = "poetry run "
        commands = {"format": r + "ruff format --check .", "lint": r + "ruff check .", "typecheck": r + "mypy .",
                    "test": r + "pytest", "build": "poetry build"}
        notes.append("Lint, type check and test run ruff, mypy and pytest through Poetry, so add them as "
                     "dev dependencies.")
    else:
        setup.append(step("Install dependencies", run="python -m pip install --upgrade pip\n"
                                                      "pip install -r requirements.txt"))
        commands = {"format": "pip install ruff && ruff format --check .", "lint": "pip install ruff && ruff check .",
                    "typecheck": "pip install mypy && mypy .", "test": "pip install pytest && pytest",
                    "build": "pip install build && python -m build"}
    return Toolchain(setup, commands, notes, ["actions/setup-python"])


def _java(pm: str, version: str | None, v: dict) -> Toolchain:
    setup = [step("Set up Java", uses=pins.pin("actions/setup-java", v),
                  with_={"distribution": "temurin", "java-version": version or "21", "cache": pm})]
    if pm == "gradle":
        commands = {"test": "./gradlew test", "build": "./gradlew build -x test"}
    else:
        commands = {"test": "mvn -B -ntp test", "build": "mvn -B -ntp package -DskipTests"}
    return Toolchain(setup, commands, ["Java has no standard lint, format or type check step; add Checkstyle or "
                                       "Spotless to your build to get one."], ["actions/setup-java"])


def _go(pm: str, version: str | None, v: dict) -> Toolchain:
    with_ = {"go-version": version} if version else {"go-version-file": "go.mod"}
    setup = [step("Set up Go", uses=pins.pin("actions/setup-go", v), with_=with_)]
    commands = {"format": 'test -z "$(gofmt -l .)"', "lint": "go vet ./...", "test": "go test ./...",
                "build": "go build ./..."}
    return Toolchain(setup, commands, [] if version else ["The Go version is read from go.mod."],
                     ["actions/setup-go"])


def _other(pm: str, version: str | None, v: dict) -> Toolchain:
    commands = {s: f'echo "TODO: add your {s} command"' for s in STEP_ORDER}
    return Toolchain([], commands, ["I don't have a template for this language yet, so the steps are "
                                    "placeholders: replace each `echo` with your real command."])


TOOLCHAINS = {"node": _node, "python": _python, "java": _java, "go": _go, "other": _other}


# ---------------------------------------------------------------- deploy jobs

def _deploy_header(branch: str) -> str:
    return "\n".join([
        "  deploy:",
        "    needs: ci",
        f"    if: {DEPLOY_IF.format(ref=f'refs/heads/{branch}')}",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 20",
        "    environment: production",
        "    permissions:",
        "      contents: read",
        "      id-token: write  # lets the job sign in to AWS with OIDC (no stored AWS keys)",
        "    steps:",
    ])


def _aws_login(v: dict) -> str:
    return step("Sign in to AWS", uses=pins.pin("aws-actions/configure-aws-credentials", v),
                with_={"role-to-assume": "${{ secrets.AWS_ROLE_ARN }}", "aws-region": "${{ env.AWS_REGION }}"})


def _deploy_s3(v, tc, language, pm) -> tuple[list[str], list[str]]:
    steps = [
        step("Download the build", uses=pins.pin("actions/download-artifact", v),
             with_={"name": "site", "path": "site"}),
        _aws_login(v),
        step("Upload to S3", run='aws s3 sync site "s3://${{ vars.S3_BUCKET }}" --delete'),
        step("Clear the CloudFront cache", if_="vars.CLOUDFRONT_DISTRIBUTION_ID != ''",
             run='aws cloudfront create-invalidation --distribution-id "${{ vars.CLOUDFRONT_DISTRIBUTION_ID }}" '
                 '--paths "/*"'),
    ]
    setup = ["Repository variable `S3_BUCKET`: the bucket that hosts the site.",
             "Optional variable `CLOUDFRONT_DISTRIBUTION_ID`: clears the CDN cache after each deploy.",
             "Set `BUILD_DIR` at the top of the workflow if your build doesn't go to `dist/`."]
    return steps, setup


def _deploy_lambda(v, tc, language, pm) -> tuple[list[str], list[str]]:
    prod_install = {"npm": "npm ci --omit=dev", "yarn": "yarn install --frozen-lockfile --production",
                    "pnpm": "pnpm install --prod --frozen-lockfile"}.get(pm, "")
    package = {
        "node": f"{prod_install}\nzip -qr function.zip . -x '.git/*' '.github/*'",
        "python": ("pip install . --target package" if pm == "poetry" else
                   "pip install -r requirements.txt --target package\n"
                   "rsync -a --exclude '.git*' --exclude package ./ package/")
                  + "\ncd package && zip -qr ../function.zip .",
        "go": "GOOS=linux GOARCH=arm64 go build -tags lambda.norpc -o bootstrap .\n"
              "zip function.zip bootstrap",
        "java": "mvn -B -ntp package -DskipTests\ncp target/*.jar function.zip" if pm == "maven"
                else "./gradlew build -x test\ncp build/libs/*.jar function.zip",
    }.get(language, 'echo "TODO: build function.zip"')
    steps = [step("Check out the code", uses=pins.pin("actions/checkout", v))]
    steps += [s for s in tc.setup if "Install dependencies" not in s]
    steps += [
        step("Package the function", run=package),
        _aws_login(v),
        step("Update the function code",
             run='aws lambda update-function-code --function-name "${{ vars.LAMBDA_FUNCTION_NAME }}" '
                 '--zip-file fileb://function.zip\n'
                 'aws lambda wait function-updated --function-name "${{ vars.LAMBDA_FUNCTION_NAME }}"'),
    ]
    setup = ["Repository variable `LAMBDA_FUNCTION_NAME`: the function to update (it must already exist; "
             "the Lambda deployment task can create it)."]
    if language == "go":
        setup.append("The Go build targets the `provided.al2023` runtime on arm64; change `GOARCH` for x86_64.")
    return steps, setup


def _deploy_ecs(v, tc, language, pm) -> tuple[list[str], list[str]]:
    steps = [
        step("Check out the code", uses=pins.pin("actions/checkout", v)),
        _aws_login(v),
        step("Sign in to Amazon ECR", id="ecr", uses=pins.pin("aws-actions/amazon-ecr-login", v)),
        step("Build and push the image", id="image",
             env={"IMAGE": "${{ steps.ecr.outputs.registry }}/${{ vars.ECR_REPOSITORY }}:${{ github.sha }}"},
             run='docker build -t "$IMAGE" .\ndocker push "$IMAGE"\necho "image=$IMAGE" >> "$GITHUB_OUTPUT"'),
        step("Put the new image in the task definition", id="task",
             uses=pins.pin("aws-actions/amazon-ecs-render-task-definition", v),
             with_={"task-definition": ".aws/task-definition.json",
                    "container-name": "${{ vars.CONTAINER_NAME }}", "image": "${{ steps.image.outputs.image }}"}),
        step("Deploy to ECS", uses=pins.pin("aws-actions/amazon-ecs-deploy-task-definition", v),
             with_={"task-definition": "${{ steps.task.outputs.task-definition }}",
                    "service": "${{ vars.ECS_SERVICE }}", "cluster": "${{ vars.ECS_CLUSTER }}",
                    "wait-for-service-stability": True}),
    ]
    setup = ["Repository variables `ECR_REPOSITORY`, `ECS_CLUSTER`, `ECS_SERVICE` and `CONTAINER_NAME`.",
             "Your task definition JSON at `.aws/task-definition.json` (export it with "
             "`aws ecs describe-task-definition --task-definition <name> --query taskDefinition`).",
             "A `Dockerfile` at the repo root (the Dockerfile task can write one)."]
    return steps, setup


DEPLOYS = {"s3_static_site": _deploy_s3, "lambda": _deploy_lambda, "ecs": _deploy_ecs}
DEPLOY_LABELS = {"s3_static_site": "uploads the build to S3 (and clears CloudFront)",
                 "lambda": "updates your Lambda function's code", "ecs": "builds a Docker image, pushes it to "
                 "ECR and rolls it out to your ECS service"}


# ---------------------------------------------------------------- the generator

def generate(params: dict, versions: dict | None = None) -> Plan:
    v = versions or {}
    notes: list[str] = []
    language = params.get("language") or "other"
    pm = params.get("package_manager") or DEFAULT_PM.get(language, "")
    if language in PMS and pm not in PMS[language]:
        notes.append(f"`{pm}` isn't a {language} package manager, so I used {DEFAULT_PM[language]}.")
        pm = DEFAULT_PM[language]
    version = _major_version(params.get("runtime_version"), None) if language != "other" else None
    deploy = params.get("deploy_target") or "none"
    steps, unknown = parse_steps(params.get("steps"))
    if unknown:
        notes.append("I didn't recognise these steps, so they're left out: " + ", ".join(unknown) + ".")
    triggers = parse_triggers(params.get("triggers"))
    branch = triggers.branches[0]

    tc = TOOLCHAINS.get(language, _other)(pm, version, v)
    notes += tc.notes
    skipped = [s for s in steps if s not in tc.commands]
    if skipped and language != "other":
        notes.append(f"No standard {', '.join(skipped)} step for {language}, so it's left out.")
    steps = [s for s in steps if s in tc.commands]
    if deploy != "none" and not triggers.push:
        notes.append(f"The deploy job runs on pushes to {branch} and manual runs, so with these triggers "
                     "it only deploys when you run the workflow by hand.")
    if deploy == "s3_static_site" and "build" not in steps and "build" in tc.commands:
        steps.append("build")
        notes.append("Added a build step, because the S3 deploy uploads the build output.")

    ci = [step("Check out the code", uses=pins.pin("actions/checkout", v))] + tc.setup
    titles = {"format": "Check formatting", "lint": "Lint", "typecheck": "Type check", "test": "Test",
              "build": "Build"}
    ci += [step(titles[s], run=tc.commands[s]) for s in steps]
    if deploy == "s3_static_site":
        ci.append(step("Save the build for the deploy job", uses=pins.pin("actions/upload-artifact", v),
                       if_=DEPLOY_IF.format(ref=f"refs/heads/{branch}"),
                       with_={"name": "site", "path": "${{ env.BUILD_DIR }}", "if-no-files-found": "error"}))

    env = ["env:"]
    if deploy != "none":
        env.append("  AWS_REGION: us-east-1")
    if deploy == "s3_static_site":
        env.append("  BUILD_DIR: dist  # folder your build writes the site to")
    header = [
        f"# Generated by InfraPilot: {_describe(language, pm, version, steps, deploy)}",
        f"name: {'CI/CD' if deploy != 'none' else 'CI'}",
        "",
        triggers.render(),
        "",
        "permissions:",
        "  contents: read",
        "",
        "# A new push to the same branch or PR cancels the run in progress; deploys are never cancelled.",
        "concurrency:",
        "  group: ${{ github.workflow }}-${{ github.ref }}",
        "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
        "",
    ]
    if len(env) > 1:
        header += env + [""]
    body = ["jobs:", "  ci:", "    runs-on: ubuntu-latest", "    timeout-minutes: 15", "    steps:",
            "\n".join(_indent(s, 6) for s in ci)]

    setup, docs = [], list(DOCS)
    if deploy in DEPLOYS:
        deploy_steps, setup = DEPLOYS[deploy](v, tc, language, pm)
        body += ["", _deploy_header(branch), "\n".join(_indent(s, 6) for s in deploy_steps)]
        setup = ["An IAM role that GitHub can assume with OIDC, saved as the repository secret "
                 "`AWS_ROLE_ARN`. Its trust policy should allow only this repo's "
                 f"`{branch}` branch and the `production` environment."] + setup
        setup.append("Change `AWS_REGION` at the top of the workflow if you don't deploy to us-east-1.")
        docs += OIDC_DOCS + (ECS_DOCS if deploy == "ecs" else [])

    content = "\n".join(header + body) + "\n"
    files = [PlanFile(path=PATH, content=content)]
    used = sorted({a for a, _ in pins.USES.findall(content)})
    plan = Plan(
        intent="cicd_github_actions", params=params, files=files,
        summary=_summary(language, steps, triggers, deploy),
        setup=setup, notes=notes,
        citations=docs + [Source(title=f"{a} releases", url=pins.source_url(a, v)) for a in used],
        estimated_cost_note="Free for public repos; private repos use your GitHub Actions minutes "
                            "(2,000 free per month on the Free plan).",
    )
    plan.validations = [pins.check_pins(content, v), *validate.run(files)]
    return plan


def _describe(language, pm, version, steps, deploy) -> str:
    lang = {"node": "Node.js", "python": "Python", "java": "Java", "go": "Go"}.get(language, "your project")
    ver = f" {version}" if version else ""
    out = f"{lang}{ver} ({pm}): {', '.join(steps)}" if language != "other" else f"{lang}: {', '.join(steps)}"
    return out + (f", then deploy ({deploy})" if deploy != "none" else "")


def _summary(language, steps, triggers: Triggers, deploy) -> str:
    when = []
    if triggers.push:
        when.append(f"pushes to {', '.join(triggers.branches)}")
    if triggers.pull_request:
        when.append("pull requests")
    if triggers.tags:
        when.append("version tags")
    if triggers.schedule:
        when.append("a daily schedule")
    lines = [f"`{PATH}` runs {', '.join(steps)} on {' and '.join(when) or 'demand'} (or by hand)."]
    if deploy in DEPLOY_LABELS:
        lines.append(f"After CI passes on {triggers.branches[0]}, a deploy job {DEPLOY_LABELS[deploy]}. "
                     "It signs in to AWS with OIDC, so no AWS keys are stored in GitHub.")
    return " ".join(lines)
