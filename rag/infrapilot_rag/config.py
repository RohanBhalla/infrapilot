"""Settings for retrieval and answering. Override with environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from the repo's .env (git-ignored). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if value[:1] in "\"'" and value[:1] and value.count(value[0]) >= 2:
                value = value[1:value.index(value[0], 1)]   # quoted: keep as is
            else:
                value = value.split(" #", 1)[0].strip()      # unquoted: drop inline comment
            os.environ.setdefault(key.strip().removeprefix("export "), value)


_load_dotenv(REPO_ROOT / ".env")
DATA_DIR = Path(os.environ.get("INFRAPILOT_DATA_DIR", REPO_ROOT / "data_collection" / "data"))

# Short names for answer models. OpenAI models need OPENAI_API_KEY (in the environment or
# the repo's .env). Nova runs on Bedrock with our AWS credentials, no key needed.
# Embeddings stay on Titan V2 either way: only the answer-writing model changes.
MODELS = {
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5": "gpt-5",
    "gpt-4.1-mini": "gpt-4.1-mini",
    "nova-pro": "us.amazon.nova-pro-v1:0",
    "nova-lite": "us.amazon.nova-lite-v1:0",
}
# USD per 1M tokens (input, output), for the cost line printed with each answer.
# Estimates from pricing pages and trackers (Oct 2026): check before relying on them.
PRICES = {
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5": (1.25, 10.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.6-luna": (0.2, 1.2),
    MODELS["nova-pro"]: (0.8, 3.2),
    MODELS["nova-lite"]: (0.06, 0.24),
}


def is_openai(model_id: str) -> bool:
    return model_id.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))


@dataclass(frozen=True)
class RagConfig:
    vectors_dir: Path = DATA_DIR / "vectors"
    table: str = "chunks"
    versions_path: Path = DATA_DIR / "versions" / "versions.json"
    region: str = os.environ.get("AWS_REGION", "us-east-1")
    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    dimensions: int = 1024
    answer_model_id: str = os.environ.get("INFRAPILOT_ANSWER_MODEL", MODELS["gpt-5.4-mini"])
    kb_id: str | None = os.environ.get("INFRAPILOT_KB_ID")
    k: int = 6                # chunks put in the prompt
    candidates: int = 30      # chunks pulled from the index before diversifying
    max_per_doc: int = 2      # so one long page can't fill every slot
    hybrid: bool = os.environ.get("INFRAPILOT_HYBRID", "1") != "0"  # vector + keyword search
    rrf_k: int = 60           # reciprocal rank fusion constant (the standard default)
    min_score: float = 0.30   # cosine; on-topic questions score 0.55-0.85, off-topic <= 0.21
    max_tokens: int = 1200
    reasoning_effort: str = os.environ.get("INFRAPILOT_REASONING_EFFORT", "low")  # gpt-5 family
    temperature: float = 0.0


CONFIG = RagConfig()
