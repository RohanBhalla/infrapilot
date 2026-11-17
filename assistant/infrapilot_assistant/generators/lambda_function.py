"""Lambda generator (plan C10): a function and its trigger as a CloudFormation template, plus an optional
GitHub Actions workflow that zips the code and updates the function on every push.

Deterministic, like the other generators. The template creates the function with placeholder code;
the workflow ships the real code, so pushes never need a stack update. The function's role can only
write its own logs (and, for the S3 trigger, read uploads and write results). The deploy role trusts
only one repo's `production` environment and can only update this function.

Node and Python are supported. Some checkov rules are skipped on purpose, with the reason written next
to them in the template: they add cost or setup (VPC, KMS keys, dead-letter queues)
that a starter function usually doesn't need.
"""

from __future__ import annotations

import re
from datetime import date

from ..plan import Plan, PlanFile, Source
from . import pins, validate
from .github_actions import OIDC_DOCS, _aws_login, _indent, _scalar, step
from .static_site import SITE_DIR, _is_yes, _skip

TEMPLATE_PATH = "infra/lambda.yaml"
WORKFLOW_PATH = ".github/workflows/deploy-lambda.yml"
STACK = "lambda-function"
TRIGGERS = ("function_url", "api_gateway", "schedule", "s3_event", "manual")
HANDLER = re.compile(r"[\w-]+(/[\w-]+)*\.[A-Za-z_]\w*")
RATE = re.compile(r"rate\((\d+) ?(minute|hour|day)s?\)")
CRON = re.compile(r"cron\(([^()]+)\)")
UPLOADS, RESULTS = "uploads/", "processed/"  # S3 trigger: only new files under uploads/ start the function

DOCS = [
    Source(title="Defining Lambda function permissions with an execution role",
           url="https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html"),
    Source(title="Configuring CloudWatch log groups for Lambda functions",
           url="https://docs.aws.amazon.com/lambda/latest/dg/monitoring-cloudwatchlogs-loggroups.html"),
    Source(title="aws lambda update-function-code",
           url="https://docs.aws.amazon.com/cli/latest/reference/lambda/update-function-code.html"),
    Source(title="aws cloudformation deploy",
           url="https://docs.aws.amazon.com/cli/latest/reference/cloudformation/deploy/index.html"),
]
TRIGGER_DOCS = {
    "function_url": [Source(title="Control access to Lambda function URLs",
                            url="https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html")],
    "api_gateway": [Source(title="Create AWS Lambda proxy integrations for HTTP APIs in API Gateway",
                           url="https://docs.aws.amazon.com/apigateway/latest/developerguide/"
                               "http-api-develop-integrations-lambda.html")],
    "schedule": [Source(title="Schedule types on EventBridge Scheduler",
                        url="https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html")],
    "s3_event": [Source(title="Process Amazon S3 event notifications with Lambda",
                        url="https://docs.aws.amazon.com/lambda/latest/dg/with-s3.html")],
    "manual": [],
}
PYTHON_PACKAGING = [Source(title="Working with .zip file archives for Python Lambda functions",
                           url="https://docs.aws.amazon.com/lambda/latest/dg/python-package.html")]
NODE_PACKAGING = [Source(title="Deploy Node.js Lambda functions with .zip file archives",
                         url="https://docs.aws.amazon.com/lambda/latest/dg/nodejs-package.html")]


# ---------------------------------------------------------------- runtime

def _supported(v: dict, language: str) -> list[str]:
    """Runtime ids of a language that Lambda still supports, newest first."""
    today = date.today().isoformat()
    found = []
    for tool, fact in v.items():
        m = re.fullmatch(r"lambda-runtime-(nodejs(\d+)\.x|python3\.(\d+))", tool)
        if not m or (language == "node") != bool(m.group(2)):
            continue
        extra = fact.get("extra") or {}
        if extra.get("status") == "supported" and not (extra.get("deprecation") or "9999") <= today:
            found.append((int(m.group(2) or m.group(3)), m.group(1)))
    return [rid for _, rid in sorted(found, reverse=True)]


def _runtime(raw, v: dict, notes: list[str]) -> tuple[str, str]:
    """(runtime id, language) from what the user said: "python", "Python 3.13", "node 22", "nodejs24.x"."""
    s = re.sub(r"\s+", "", str(raw or "")).lower()  # nothing said: Node
    lts = ((v.get("nodejs") or {}).get("lts") or "").split(".")[0]
    if m := re.fullmatch(r"(?:nodejs|node(?:\.js)?|javascript|typescript|js|ts)(\d+)?(?:\.x)?", s or "node"):
        language, wanted = "node", f"nodejs{m.group(1) or lts or 24}.x"
    elif m := re.fullmatch(r"(?:python|py)(3(?:\.\d+)?)?", s):
        language, wanted = "python", f"python{m.group(1)}" if m.group(1) and "." in m.group(1) else None
    else:
        notes.append(f"I can only set up Node.js and Python functions so far, so I used Node.js instead of "
                     f"`{raw}`.")
        language, wanted = "node", f"nodejs{lts or 24}.x"

    supported = _supported(v, language)
    if not supported:  # registry has no runtime facts: trust the user's version
        return wanted or "python3.13", language
    if wanted is None:
        return supported[0], language
    if wanted not in supported:
        notes.append(f"Lambda no longer supports (or doesn't offer) `{wanted}`, so I used `{supported[0]}`.")
        return supported[0], language
    return wanted, language


def _schedule(raw, notes: list[str]) -> str:
    s = str(raw or "").strip()
    if m := RATE.fullmatch(s):
        n, unit = int(m.group(1)), m.group(2)
        if n > 0:
            return f"rate({n} {unit}{'' if n == 1 else 's'})"  # EventBridge rejects "rate(1 hours)"
    if (m := CRON.fullmatch(s)) and len(m.group(1).split()) == 6:
        return s
    notes.append(f"`{raw}` isn't a schedule expression I can use, so it runs every hour (`rate(1 hour)`); "
                 "change `ScheduleExpression` in the template.")
    return "rate(1 hour)"


# ---------------------------------------------------------------- template

# The upload bucket's name is fixed up front (account id + part of the stack id) so the role and the
# invoke permission can name it before the bucket exists; that avoids a circular dependency.
UPLOAD_BUCKET_PREFIX = "uploads"  # the sandbox swaps this for its stack name (sandbox.py)
BUCKET_NAME = (f'!Join ["-", [{UPLOAD_BUCKET_PREFIX}, !Ref AWS::AccountId, '
               '!Select [0, !Split ["-", !Select [2, !Split ["/", !Ref AWS::StackId]]]]]]')


def _bucket_arn(path: str = "") -> str:
    return f'!Sub ["arn:${{AWS::Partition}}:s3:::${{Name}}{"/" + path if path else ""}", {{Name: {BUCKET_NAME}}}]'


def _placeholder(language: str, function: str) -> list[str]:
    """Inline code so the stack can be created before the first push. CloudFormation names the file
    index.js / index.py."""
    msg = "Deployed by InfraPilot. Push your code to replace this placeholder."
    if language == "python":
        return [f"def {function}(event, context):", f'    return {{"statusCode": 200, "body": "{msg}"}}']
    return [f'exports.{function} = async () => ({{ statusCode: 200, body: "{msg}" }});']


def _template(trigger: str, runtime: str, language: str, handler: str, memory: int, timeout: int,
              schedule: str | None, ci: bool) -> str:
    out = [
        f"# Generated by InfraPilot: a Lambda function ({runtime}) with {_trigger_label(trigger)}.",
        'AWSTemplateFormatVersion: "2010-09-09"',
        f"Description: Lambda function with {_trigger_label(trigger)}",
    ]
    if ci:
        out += [
            "",
            "Parameters:",
            "  GitHubRepository:",
            "    Type: String",
            '    Default: ""',
            "    AllowedPattern: ^$|^[\\w.-]+/[\\w.-]+$",
            "    Description: owner/repo allowed to deploy (creates a GitHub OIDC deploy role); empty for none",
            "",
            "Conditions:",
            "  HasGitHubRepository: !Not [!Equals [!Ref GitHubRepository, \"\"]]",
        ]

    out += ["", "Resources:", "  LogGroup:", "    Type: AWS::Logs::LogGroup"]
    out += _skip(("CKV_AWS_158", "A customer managed KMS key costs $1/month; CloudWatch encrypts logs by "
                                 "default. Add KmsKeyId if your logs hold sensitive data."))
    out += [
        "    Properties:",
        "      RetentionInDays: 30",
        "",
        "  # What the function itself may do: write its own logs"
        + (", read new uploads and write results." if trigger == "s3_event" else ", nothing else."),
        "  FunctionRole:",
        "    Type: AWS::IAM::Role",
        "    Properties:",
        "      AssumeRolePolicyDocument:",
        '        Version: "2012-10-17"',
        "        Statement:",
        "          - Effect: Allow",
        "            Principal:",
        "              Service: lambda.amazonaws.com",
        "            Action: sts:AssumeRole",
        "      Policies:",
        "        - PolicyName: function",
        "          PolicyDocument:",
        '            Version: "2012-10-17"',
        "            Statement:",
        "              - Effect: Allow",
        "                Action: [logs:CreateLogStream, logs:PutLogEvents]",
        "                Resource: !GetAtt LogGroup.Arn",
    ]
    if trigger == "s3_event":
        out += [
            "              - Effect: Allow",
            "                Action: s3:GetObject",
            f"                Resource: {_bucket_arn(UPLOADS + '*')}",
            "              - Effect: Allow",
            "                Action: s3:PutObject",
            f"                Resource: {_bucket_arn(RESULTS + '*')}",
        ]

    fn = handler.rsplit(".", 1)[1]
    out += ["", "  Function:", "    Type: AWS::Lambda::Function"]
    out += _skip(
        ("CKV_AWS_116", "A dead-letter queue only applies to async invokes and needs an SQS queue; add "
                        "DeadLetterConfig if failed events must be kept."),
        ("CKV_AWS_117", "Running in a VPC is only needed to reach private resources and usually needs a NAT "
                        "gateway (about $32/month) for internet access."),
        ("CKV_AWS_115", "Reserved concurrency takes capacity from the account's pool, which is small in new "
                        "accounts; set ReservedConcurrentExecutions to cap the function."),
    )
    out += [
        "    Properties:",
        f"      Runtime: {runtime}",
        f"      Handler: {handler}",
        "      Architectures: [arm64]  # Graviton: about 20% cheaper than x86_64",
        f"      MemorySize: {memory}",
        f"      Timeout: {timeout}",
        "      Role: !GetAtt FunctionRole.Arn",
        "      LoggingConfig:",
        "        LogGroup: !Ref LogGroup",
        "      # Placeholder until the first deploy uploads your code. Leave it as is: CloudFormation only",
        "      # replaces the code when this text changes.",
        "      Code:",
        "        ZipFile: |",
        *(f"          {line}" for line in _placeholder(language, fn)),
    ]

    out += _trigger_resources(trigger, schedule)

    if ci:
        out += [
            "",
            "  # Lets the GitHub Actions workflow update this function's code, and nothing else. It trusts",
            "  # only the repo's `production` environment. Needs the GitHub OIDC provider in this account (once).",
            "  GitHubDeployRole:",
            "    Type: AWS::IAM::Role",
            "    Condition: HasGitHubRepository",
            "    Properties:",
            "      AssumeRolePolicyDocument:",
            '        Version: "2012-10-17"',
            "        Statement:",
            "          - Effect: Allow",
            "            Principal:",
            "              Federated: !Sub arn:${AWS::Partition}:iam::${AWS::AccountId}:oidc-provider/token.actions.githubusercontent.com",
            "            Action: sts:AssumeRoleWithWebIdentity",
            "            Condition:",
            "              StringEquals:",
            "                token.actions.githubusercontent.com:aud: sts.amazonaws.com",
            "                token.actions.githubusercontent.com:sub: !Sub repo:${GitHubRepository}:environment:production",
            "      Policies:",
            "        - PolicyName: deploy-function",
            "          PolicyDocument:",
            '            Version: "2012-10-17"',
            "            Statement:",
            "              - Effect: Allow",
            "                Action: [lambda:UpdateFunctionCode, lambda:GetFunction, lambda:GetFunctionConfiguration]",
            "                Resource: !GetAtt Function.Arn",
            "              - Effect: Allow",
            "                Action: cloudformation:DescribeStacks",
            "                Resource: !Ref AWS::StackId",
        ]

    out += ["", "Outputs:", "  FunctionName:", "    Value: !Ref Function", "  FunctionArn:",
            "    Value: !GetAtt Function.Arn"]
    if trigger == "function_url":
        out += ["  FunctionUrl:", "    Value: !GetAtt FunctionUrl.FunctionUrl"]
    elif trigger == "api_gateway":
        out += ["  ApiUrl:", "    Value: !GetAtt Api.ApiEndpoint"]
    elif trigger == "s3_event":
        out += ["  UploadBucket:", f"    Description: Upload files under {UPLOADS} to run the function",
                "    Value: !Ref UploadBucket"]
    if ci:
        out += ["  DeployRoleArn:", "    Condition: HasGitHubRepository",
                "    Description: Save as the repository secret AWS_ROLE_ARN",
                "    Value: !GetAtt GitHubDeployRole.Arn"]
    return "\n".join(out) + "\n"


def _trigger_label(trigger: str) -> str:
    return {"function_url": "a public HTTPS URL", "api_gateway": "an API Gateway HTTP API",
            "schedule": "a schedule", "s3_event": "an S3 upload trigger",
            "manual": "no trigger (invoked directly)"}[trigger]


def _invoke_permission(name: str, principal: str, *extra: str) -> list[str]:
    return ["", f"  {name}:", "    Type: AWS::Lambda::Permission", "    Properties:",
            "      FunctionName: !Ref Function", "      Action: lambda:InvokeFunction",
            f"      Principal: {principal}", *extra]


def _trigger_resources(trigger: str, schedule: str | None) -> list[str]:
    if trigger == "function_url":
        out = ["", "  # Anyone with the URL can call the function. Set AuthType to AWS_IAM to require signed",
               "  # requests instead (and drop the two permissions below).",
               "  FunctionUrl:", "    Type: AWS::Lambda::Url"]
        out += _skip(("CKV_AWS_258", "A public URL was asked for; set AuthType to AWS_IAM to require "
                                     "signed requests."))
        out += ["    Properties:", "      TargetFunctionArn: !GetAtt Function.Arn", "      AuthType: NONE",
                "", "  # Public function URLs need both of these permissions.",
                "  FunctionUrlPermission:", "    Type: AWS::Lambda::Permission",
                "    Properties:", "      FunctionName: !Ref Function", "      Action: lambda:InvokeFunctionUrl",
                '      Principal: "*"', "      FunctionUrlAuthType: NONE",
                "", "  FunctionUrlInvokePermission:", "    Type: AWS::Lambda::Permission",
                "    Properties:", "      FunctionName: !Ref Function", "      Action: lambda:InvokeFunction",
                '      Principal: "*"', "      InvokedViaFunctionUrl: true"]
        return out

    if trigger == "api_gateway":
        return [
            "",
            "  # Quick create: one catch-all route and a default stage that send every request to the function.",
            "  Api:",
            "    Type: AWS::ApiGatewayV2::Api",
            "    Properties:",
            "      Name: !Sub ${AWS::StackName}-api",
            "      ProtocolType: HTTP",
            "      Target: !GetAtt Function.Arn",
            *_invoke_permission(
                "ApiPermission", "apigateway.amazonaws.com",
                "      SourceArn: !Sub arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${Api}/*"),
        ]

    if trigger == "schedule":
        return [
            "",
            "  # Lets EventBridge Scheduler invoke the function, and nothing else.",
            "  SchedulerRole:",
            "    Type: AWS::IAM::Role",
            "    Properties:",
            "      AssumeRolePolicyDocument:",
            '        Version: "2012-10-17"',
            "        Statement:",
            "          - Effect: Allow",
            "            Principal:",
            "              Service: scheduler.amazonaws.com",
            "            Action: sts:AssumeRole",
            "            Condition:",
            "              StringEquals:",
            "                aws:SourceAccount: !Ref AWS::AccountId",
            "      Policies:",
            "        - PolicyName: invoke-function",
            "          PolicyDocument:",
            '            Version: "2012-10-17"',
            "            Statement:",
            "              - Effect: Allow",
            "                Action: lambda:InvokeFunction",
            "                Resource: !GetAtt Function.Arn",
            "",
            "  Schedule:",
            "    Type: AWS::Scheduler::Schedule",
            "    Properties:",
            f"      ScheduleExpression: {_scalar(schedule)}",
            "      ScheduleExpressionTimezone: UTC",
            "      FlexibleTimeWindow:",
            '        Mode: "OFF"',
            "      Target:",
            "        Arn: !GetAtt Function.Arn",
            "        RoleArn: !GetAtt SchedulerRole.Arn",
            "        RetryPolicy:",
            "          MaximumRetryAttempts: 2",
        ]

    if trigger == "s3_event":
        out = _invoke_permission("UploadPermission", "s3.amazonaws.com",
                                 "      SourceAccount: !Ref AWS::AccountId", f"      SourceArn: {_bucket_arn()}")
        out += ["", f"  # New files under {UPLOADS} run the function. Write results somewhere else (the role allows "
                f"{RESULTS}),", "  # or each result would run the function again.",
                "  UploadBucket:", "    Type: AWS::S3::Bucket", "    DependsOn: UploadPermission"]
        out += _skip(("CKV_AWS_18", "Access logs need a second bucket and cost extra; add logging if you "
                                    "need an audit trail of uploads."))
        out += [
            "    Properties:",
            f"      BucketName: {BUCKET_NAME}",
            "      BucketEncryption:",
            "        ServerSideEncryptionConfiguration:",
            "          - ServerSideEncryptionByDefault:",
            "              SSEAlgorithm: AES256",
            "      PublicAccessBlockConfiguration:",
            "        BlockPublicAcls: true",
            "        BlockPublicPolicy: true",
            "        IgnorePublicAcls: true",
            "        RestrictPublicBuckets: true",
            "      OwnershipControls:",
            "        Rules:",
            "          - ObjectOwnership: BucketOwnerEnforced",
            "      VersioningConfiguration:",
            "        Status: Enabled  # recover overwritten files; old versions are deleted after 30 days",
            "      LifecycleConfiguration:",
            "        Rules:",
            "          - Id: expire-old-versions",
            "            Status: Enabled",
            "            NoncurrentVersionExpiration:",
            "              NoncurrentDays: 30",
            "            AbortIncompleteMultipartUpload:",
            "              DaysAfterInitiation: 1",
            "      NotificationConfiguration:",
            "        LambdaConfigurations:",
            "          - Event: s3:ObjectCreated:*",
            "            Function: !GetAtt Function.Arn",
            "            Filter:",
            "              S3Key:",
            "                Rules:",
            "                  - Name: prefix",
            f"                    Value: {UPLOADS}",
            "",
            "  UploadBucketPolicy:",
            "    Type: AWS::S3::BucketPolicy",
            "    Properties:",
            "      Bucket: !Ref UploadBucket",
            "      PolicyDocument:",
            '        Version: "2012-10-17"',
            "        Statement:",
            "          - Sid: HttpsOnly",
            "            Effect: Deny",
            '            Principal: "*"',
            "            Action: s3:*",
            "            Resource:",
            "              - !GetAtt UploadBucket.Arn",
            "              - !Sub ${UploadBucket.Arn}/*",
            "            Condition:",
            "              Bool:",
            "                aws:SecureTransport: false",
        ]
        return out
    return []


# ---------------------------------------------------------------- workflow

def _workflow(code_dir: str, runtime: str, language: str, region: str, v: dict) -> str:
    steps = [step("Check out the code", uses=pins.pin("actions/checkout", v))]
    if language == "python":
        version = runtime.removeprefix("python")
        steps += [
            step("Set up Python", uses=pins.pin("actions/setup-python", v), with_={"python-version": version}),
            # Lambda runs on arm64 Linux, so dependencies are installed as arm64 wheels.
            step("Package the function", run="\n".join([
                'pkg="$RUNNER_TEMP/package"  # outside the repo, so a CODE_DIR of "." still works',
                'mkdir -p "$pkg" && cp -R "$CODE_DIR"/. "$pkg"/',
                'if [ -f "$pkg/requirements.txt" ]; then',
                '  pip install -r "$pkg/requirements.txt" --target "$pkg" --implementation cp \\',
                f'    --python-version "{version}" --only-binary=:all: \\',
                "    --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64",
                "fi",
                "cd \"$pkg\" && zip -qr \"$GITHUB_WORKSPACE/function.zip\" . -x '*.pyc' '*/__pycache__/*' '.git/*'"])),
        ]
    else:
        steps += [
            step("Set up Node.js", uses=pins.pin("actions/setup-node", v),
                 with_={"node-version": runtime.removeprefix("nodejs").removesuffix(".x")}),
            step("Package the function", run="\n".join([
                'cd "$CODE_DIR"',
                "if [ -f package-lock.json ]; then npm ci --omit=dev",
                "elif [ -f package.json ]; then npm install --omit=dev; fi",
                "zip -qr \"$GITHUB_WORKSPACE/function.zip\" . -x '.git/*'"])),
        ]
    steps += [
        _aws_login(v),
        step("Find the function", id="stack", run="\n".join([
            'name=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \\',
            "  --query \"Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue\" --output text)",
            'echo "function=$name" >> "$GITHUB_OUTPUT"'])),
        step("Update the function code", env={"FUNCTION": "${{ steps.stack.outputs.function }}"}, run="\n".join([
            'aws lambda update-function-code --function-name "$FUNCTION" --zip-file fileb://function.zip',
            'aws lambda wait function-updated --function-name "$FUNCTION"'])),
    ]
    return "\n".join([
        "# Generated by InfraPilot: zips the function code and deploys it to the lambda-function stack on each push.",
        "name: Deploy function",
        "",
        "on:",
        "  push:",
        "    branches: [main]",
        "  workflow_dispatch:  # also lets you run it by hand from the Actions tab",
        "",
        "permissions:",
        "  contents: read",
        "",
        "# One deploy at a time; a newer push waits instead of cancelling an update halfway.",
        "concurrency:",
        "  group: deploy-lambda",
        "  cancel-in-progress: false",
        "",
        "env:",
        f"  AWS_REGION: {region}",
        f"  STACK_NAME: {STACK}",
        f"  CODE_DIR: {_scalar(code_dir)}  # this folder becomes the root of the zip",
        "",
        "jobs:",
        "  deploy:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 15",
        "    environment: production",
        "    permissions:",
        "      contents: read",
        "      id-token: write  # lets the job sign in to AWS with OIDC (no stored AWS keys)",
        "    steps:",
        "\n".join(_indent(s, 6) for s in steps),
    ]) + "\n"


# ---------------------------------------------------------------- plan

def _int(value, default: int, low: int, high: int, label: str, notes: list[str]) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if not low <= n <= high:
        notes.append(f"{label} must be between {low} and {high}, so I used {min(max(n, low), high)}.")
        n = min(max(n, low), high)
    return n


def generate(params: dict, versions: dict | None = None) -> Plan:
    v = versions or {}
    notes: list[str] = []

    trigger = params.get("trigger") if params.get("trigger") in TRIGGERS else "manual"
    if params.get("trigger") not in TRIGGERS:
        notes.append(f"I don't know the trigger `{params.get('trigger')}`, so nothing triggers the function; "
                     "you invoke it yourself.")
    runtime, language = _runtime(params.get("runtime"), v, notes)
    code_dir = str(params.get("source_dir") or "src").strip().strip("/").removeprefix("./") or "."
    if code_dir != "." and (not SITE_DIR.fullmatch(code_dir) or ".." in code_dir.split("/")):
        notes.append(f"`{params.get('source_dir')}` doesn't look like a folder path, so I used `src`.")
        code_dir = "src"
    handler = str(params.get("handler") or "index.handler").strip()
    if not HANDLER.fullmatch(handler):
        notes.append(f"`{handler}` isn't a handler like `file.function`, so I used `index.handler`.")
        handler = "index.handler"
    memory = _int(params.get("memory_mb"), 256, 128, 10240, "Memory", notes)
    timeout = _int(params.get("timeout_s"), 10, 1, 900, "The timeout", notes)
    schedule = _schedule(params.get("schedule"), notes) if trigger == "schedule" else None
    ci = _is_yes(params.get("ci_deploy", True))
    region = str(params.get("region") or "us-east-1").strip()
    if not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-\d", region):
        notes.append(f"`{region}` isn't an AWS region name, so I used us-east-1.")
        region = "us-east-1"

    files = [PlanFile(path=TEMPLATE_PATH, content=_template(trigger, runtime, language, handler, memory,
                                                            timeout, schedule, ci))]
    if ci:
        files.append(PlanFile(path=WORKFLOW_PATH, content=_workflow(code_dir, runtime, language, region, v)))

    ext = "py" if language == "python" else "js"
    deploy_cmd = (f"aws cloudformation deploy --template-file {TEMPLATE_PATH} --stack-name {STACK} "
                  f"--region {region} --capabilities CAPABILITY_IAM"
                  + (" --parameter-overrides GitHubRepository=<owner/repo>" if ci else ""))
    setup = []
    if ci:
        setup.append("The GitHub OIDC identity provider in your AWS account (once per account): "
                     "`token.actions.githubusercontent.com` with audience `sts.amazonaws.com`.")
    setup.append(f"Create the stack: `{deploy_cmd}`. The function starts with placeholder code.")
    if ci:
        setup.append("Save the stack's `DeployRoleArn` output as the repository secret `AWS_ROLE_ARN`, and "
                     "create a `production` environment in the repo settings. Then push to main to deploy "
                     "your code.")
    else:
        setup.append(f"Deploy your code: zip the contents of `{code_dir}/` into function.zip, then "
                     "`aws lambda update-function-code --function-name <FunctionName output> "
                     "--zip-file fileb://function.zip`.")

    if handler.split("/")[-1].split(".")[0] != "index" or "/" in handler:
        notes.append(f"The placeholder code only answers at `index.{handler.rsplit('.', 1)[1]}`, so calls fail "
                     f"until your first deploy puts `{handler.rsplit('.', 1)[0]}.{ext}` in the zip.")
    deps = "requirements.txt" if language == "python" else "package.json"
    notes.append(f"The workflow zips `{code_dir}/` as the root of the package and installs dependencies from a "
                 f"`{deps}` in that folder, so `{handler}` must be relative to it.")
    if language == "node":
        notes.append("Packages with native code must be built for arm64 Linux; most npm packages are plain "
                     "JavaScript and work as is.")
    if trigger == "function_url":
        notes.append("The function URL is public: anyone who has it can call the function (and run up "
                     "invocations). Check a secret header in your code, or switch AuthType to AWS_IAM.")
    if trigger == "api_gateway":
        notes.append("The API has no authorizer or throttling: anyone with the URL can call it. Add a JWT "
                     "authorizer or route throttling before sharing it widely.")
    if trigger == "schedule":
        notes.append(f"Runs on `{schedule}`, in UTC. Change `ScheduleExpressionTimezone` to use your own time zone.")
    if trigger == "s3_event":
        notes.append(f"Upload files under `{UPLOADS}` to run the function, and write results under `{RESULTS}`. "
                     "Deleting the stack fails while the bucket has files: empty it first.")

    docs = DOCS + TRIGGER_DOCS[trigger] + (PYTHON_PACKAGING if language == "python" else NODE_PACKAGING) \
        + (OIDC_DOCS if ci else [])
    used = sorted({a for f in files for a, _ in pins.USES.findall(f.content)})
    plan = Plan(
        intent="deploy_lambda", params=params, files=files,
        summary=_summary(trigger, runtime, code_dir, ci),
        setup=setup, notes=notes,
        citations=docs + [Source(title=f"{a} releases", url=pins.source_url(a, v)) for a in used],
        estimated_cost_note=_cost(trigger),
    )
    workflows = "\n".join(f.content for f in files if f.path == WORKFLOW_PATH)
    plan.validations = ([pins.check_pins(workflows, v)] if workflows else []) + validate.run(files)
    return plan


def _cost(trigger: str) -> str:
    base = ("Lambda's free tier covers 1 million requests and 400,000 GB-seconds a month; past that it's "
            "$0.20 per million requests plus run time. Logs cost $0.50/GB ingested.")
    extra = {"api_gateway": " The HTTP API is $1.00 per million requests (no free tier after the first year).",
             "schedule": " EventBridge Scheduler's free tier covers 14 million invocations a month.",
             "s3_event": " S3 storage is about $0.023/GB-month."}
    return base + extra.get(trigger, "")


def _summary(trigger: str, runtime: str, code_dir: str, ci: bool) -> str:
    parts = [f"A CloudFormation template (`{TEMPLATE_PATH}`) that creates a {runtime} Lambda function on arm64 "
             f"with {_trigger_label(trigger)}, its own least-privilege role and a log group kept for 30 days."]
    if ci:
        parts.append(f"A GitHub Actions workflow (`{WORKFLOW_PATH}`) zips `{code_dir}/` and updates the function "
                     "on every push to main, signing in to AWS with a role the template creates.")
    return " ".join(parts)
