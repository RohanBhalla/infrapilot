"""The intents InfraPilot understands and the details (slots) each task needs.

The classifier only extracts values; everything here (what's required, allowed values,
defaults, the question to ask) is plain data, so the dialog is predictable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SlotType = Literal["enum", "bool", "int", "str"]


@dataclass(frozen=True)
class Slot:
    name: str
    type: SlotType
    description: str
    question: str                       # asked when a required slot is missing
    required: bool = False
    options: tuple[str, ...] = ()       # allowed values for enum slots
    default: object = None              # value, or callable(params, versions) -> value
    min: int | None = None
    max: int | None = None
    # Consent slots (e.g. accepting hourly cost) only count as "yes" when we asked the question
    # or the user's own words mention cost; the model can't fill them in on its own.
    consent: bool = False


@dataclass(frozen=True)
class Intent:
    name: str
    description: str
    kind: Literal["answer", "task", "other"]
    slots: tuple[Slot, ...] = field(default_factory=tuple)

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)


def _node_lts(params, versions) -> str | None:
    fact = versions.get("nodejs") if versions else None
    return fact["lts"].split(".")[0] if fact and fact.get("lts") else None


def _lambda_node_runtime(params, versions) -> str | None:
    lts = _node_lts(params, versions)
    return f"nodejs{lts}.x" if lts else None


def _eks_version(params, versions) -> str | None:
    fact = versions.get("amazon-eks") if versions else None
    return ".".join(fact["latest"].split("-")[0].split(".")[:2]) if fact and fact.get("latest") else None


def _start_command(params, versions) -> str:
    return {"node": "npm start", "python": "python main.py", "java": "java -jar app.jar",
            "go": "./app"}.get(params.get("language"), "npm start")


def _package_manager(params, versions) -> str:
    return {"node": "npm", "python": "pip", "java": "maven", "go": "go"}.get(params.get("language"), "npm")


def _spa_default(params, versions) -> bool:
    return params.get("framework") in ("react", "vue", "angular", "svelte")


LANGUAGES = ("node", "python", "java", "go", "other")
REGION = Slot("region", "str", "AWS region to deploy to", "Which AWS region?", default="us-east-1")

INTENTS: dict[str, Intent] = {i.name: i for i in [
    Intent("qa", "A question about how something works or how to do something, answered from the docs.",
           "answer"),
    Intent("explain_failure", "The user shares (or describes) a failed build, deploy, scan or error and wants "
           "the cause and fix.", "answer", (
        Slot("error_text", "str", "The error message or failing log lines, copied from the user's message",
             "Can you paste the error message or the failing part of the log?", required=True),
    )),
    Intent("cicd_github_actions", "Set up or change a GitHub Actions CI/CD workflow.", "task", (
        Slot("language", "enum", "Main language of the project",
             "What language is the project in?", required=True, options=LANGUAGES),
        Slot("package_manager", "enum", "Package manager", "Which package manager?",
             options=("npm", "yarn", "pnpm", "pip", "poetry", "maven", "gradle", "go"), default=_package_manager),
        Slot("runtime_version", "str", "Language version, e.g. Node 24", "Which version?", default=_node_lts),
        Slot("steps", "str", "Steps to run, comma separated (lint, test, build)", "Which steps should it run?",
             default="lint, test, build"),
        Slot("triggers", "str", "When it runs", "When should it run?",
             default="pushes to main and pull requests"),
        Slot("deploy_target", "enum", "Where the workflow deploys to, if anywhere", "Should it also deploy?",
             options=("none", "s3_static_site", "lambda", "ecs"), default="none"),
    )),
    Intent("containerize", "Write a Dockerfile for an app, optionally push it to ECR and run it on ECS Fargate.",
           "task", (
        Slot("language", "enum", "Main language of the app", "What language is the app written in?",
             required=True, options=LANGUAGES),
        Slot("app_port", "int", "Port the app listens on", "Which port does the app listen on?",
             required=True, min=1, max=65535),
        Slot("start_command", "str", "Command that starts the app", "What command starts the app?",
             default=_start_command),
        Slot("push_to_ecr", "bool", "Push the image to Amazon ECR", "Should I push the image to ECR?",
             default=True),
        Slot("deploy_to_ecs", "bool", "Run it as an ECS Fargate service", "Should I deploy it to ECS Fargate?",
             default=False),
    )),
    Intent("deploy_lambda", "Deploy a function to AWS Lambda.", "task", (
        Slot("trigger", "enum", "What invokes the function (manual: the user invokes it themselves)",
             "What should trigger the function: an HTTPS URL, API Gateway, a schedule, S3 uploads, or "
             "nothing (you invoke it yourself)?", required=True,
             options=("function_url", "api_gateway", "schedule", "s3_event", "manual")),
        Slot("runtime", "str", "Lambda runtime id, e.g. nodejs24.x or python3.13", "Which runtime?",
             default=_lambda_node_runtime),
        Slot("source_dir", "str", "Folder with the function code", "Which folder has the code?", default="src"),
        Slot("memory_mb", "int", "Memory in MB", "How much memory?", default=256, min=128, max=10240),
        Slot("timeout_s", "int", "Timeout in seconds", "What timeout?", default=10, min=1, max=900),
        REGION,
    )),
    Intent("deploy_static_site", "Host a static website (e.g. a React build) on S3 + CloudFront.", "task", (
        Slot("source_dir", "str", "Folder with the built site, e.g. dist or build",
             "Which folder has the built site (for example `dist/` or `build/`)?", required=True),
        Slot("framework", "enum", "Front-end framework", "Which framework?",
             options=("react", "vue", "angular", "svelte", "plain", "other"), default="plain"),
        Slot("spa_routing", "bool", "Send unknown paths to index.html (single-page app)",
             "Is it a single-page app?", default=_spa_default),
        Slot("custom_domain", "str", "Custom domain name, if any", "Do you want a custom domain?",
             default="none (CloudFront URL)"),
        Slot("ci_deploy", "bool", "Add a GitHub Actions workflow that deploys on push",
             "Should I add a GitHub Actions deploy workflow?", default=True),
        REGION,
    )),
    Intent("eks_cluster", "Create a Kubernetes cluster on Amazon EKS with autoscaling (stretch goal).", "task", (
        Slot("cost_ack", "bool", "User accepts that EKS is billed per hour while it runs",
             "EKS is billed by the hour (about $0.10/hour for the cluster plus the nodes) until it's "
             "deleted. Is that OK? We'd tear it down after testing.", required=True, consent=True),
        Slot("cluster_name", "str", "Cluster name", "What should the cluster be called?",
             default="infrapilot-demo"),
        Slot("autoscaler", "enum", "Node autoscaling", "Which node autoscaler?",
             options=("karpenter", "cluster_autoscaler"), default="karpenter"),
        Slot("k8s_version", "str", "Kubernetes version", "Which Kubernetes version?", default=_eks_version),
        Slot("node_instance_type", "str", "EC2 instance type for nodes", "Which instance type?",
             default="t3.medium"),
        Slot("max_nodes", "int", "Maximum number of nodes", "How many nodes at most?", default=3, min=1, max=20),
        REGION,
    )),
    Intent("other", "Anything outside InfraPilot's scope (AWS infrastructure and CI/CD), or chit-chat.", "other"),
]}

TASK_INTENTS = [n for n, i in INTENTS.items() if i.kind == "task"]
ALL_SLOTS = sorted({s.name for i in INTENTS.values() for s in i.slots})


def resolve_default(slot: Slot, params: dict, versions) -> object:
    d = slot.default
    return d(params, versions) if callable(d) else d


# Short labels used in the assistant's replies.
LABELS = {
    "cicd_github_actions": "a GitHub Actions workflow",
    "containerize": "a Dockerfile for your app",
    "deploy_lambda": "a Lambda deployment",
    "deploy_static_site": "static site hosting on S3 + CloudFront",
    "eks_cluster": "an EKS cluster",
}

# Common ways people name a language, mapped to the option value.
ALIASES = {
    "javascript": "node", "typescript": "node", "js": "node", "ts": "node", "nodejs": "node",
    "node.js": "node", "py": "python", "golang": "go", "kotlin": "java",
}
