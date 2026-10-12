"""Answer models: OpenAI (default) or Bedrock (Nova), behind one small interface."""

from __future__ import annotations

import time
from typing import Protocol

import os

from .config import CONFIG, MODELS, PRICES, RagConfig, is_openai


class ChatModel(Protocol):
    model_id: str

    def complete(self, system: str, user: str) -> tuple[str, dict]: ...


class BedrockChat:
    def __init__(self, model_id: str | None = None, cfg: RagConfig = CONFIG):
        import boto3
        from botocore.config import Config

        self.cfg = cfg
        self.model_id = model_id or cfg.answer_model_id
        self.client = boto3.client("bedrock-runtime", region_name=cfg.region,
                                   config=Config(retries={"max_attempts": 6, "mode": "adaptive"}))

    def complete(self, system: str, user: str) -> tuple[str, dict]:
        started = time.monotonic()
        resp = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": self.cfg.max_tokens, "temperature": self.cfg.temperature},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        usage = resp.get("usage", {})
        return text, {
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "stop_reason": resp.get("stopReason"),
        }


class OpenAIChat:
    """OpenAI Chat Completions. Needs OPENAI_API_KEY (environment or the repo's .env)."""

    def __init__(self, model_id: str | None = None, cfg: RagConfig = CONFIG):
        from openai import OpenAI

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set. Add it to the repo's .env "
                               "(see .env.example), or use --model nova-pro to answer with Bedrock.")
        self.cfg = cfg
        self.model_id = model_id or cfg.answer_model_id
        self.client = OpenAI(max_retries=4, timeout=60)
        # gpt-5 / o-series are reasoning models: no temperature, and their hidden reasoning
        # tokens count toward the completion limit, so give them more room.
        self.reasoning = not self.model_id.startswith(("gpt-4", "chatgpt-"))

    def complete(self, system: str, user: str) -> tuple[str, dict]:
        started = time.monotonic()
        kwargs = {"model": self.model_id,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]}
        if self.reasoning:
            kwargs |= {"max_completion_tokens": self.cfg.max_tokens + 4000,
                       "reasoning_effort": self.cfg.reasoning_effort}
        else:
            kwargs |= {"max_completion_tokens": self.cfg.max_tokens,
                       "temperature": self.cfg.temperature}
        resp = self.client.chat.completions.create(**kwargs)
        choice, usage = resp.choices[0], resp.usage
        details = getattr(usage, "completion_tokens_details", None)
        return choice.message.content or "", {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,  # includes reasoning tokens (billed as output)
            "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "stop_reason": choice.finish_reason,
        }


def make_chat(model: str | None = None, cfg: RagConfig = CONFIG) -> ChatModel:
    """Pick the client from the model name: gpt-*/o* -> OpenAI, anything else -> Bedrock."""
    model_id = MODELS.get(model, model) if model else cfg.answer_model_id
    return OpenAIChat(model_id, cfg) if is_openai(model_id) else BedrockChat(model_id, cfg)


def cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model_id, (0.0, 0.0))
    return round((input_tokens * pin + output_tokens * pout) / 1e6, 6)
