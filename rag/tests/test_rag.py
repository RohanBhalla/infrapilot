"""Offline tests: a fake embedder and fake LLM, so no AWS calls or cost."""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import lancedb
import pytest

from infrapilot_rag.answer import AnswerService
from infrapilot_rag.config import CONFIG
from infrapilot_rag.models import RetrievedChunk
from infrapilot_rag.prompt import REFUSAL, SYSTEM_PROMPT, build_user_message, parse_citations
from infrapilot_rag.retriever import LocalLanceRetriever, check_filters, diversify, kb_filter, lance_where
from infrapilot_rag.versions import VersionRegistry

SNAPSHOTS = Path(__file__).parent / "snapshots"
DIMS = 128


class FakeEmbedder:
    """Bag-of-words hashed into a normalized vector: shared words -> higher cosine."""

    def embed(self, text: str) -> list[float]:
        v = [0.0] * DIMS
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIMS] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        return [x / n for x in v]


class FakeLLM:
    model_id = "fake"

    def __init__(self, reply: str):
        self.reply, self.calls = reply, []

    def complete(self, system: str, user: str):
        self.calls.append(user)
        return self.reply, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 1}


CORPUS = [
    ("gh-cache", "d1", "CI/CD – GitHub Actions", "reference", "Caching dependencies",
     "Use actions/cache or setup-node cache: npm to cache npm dependencies in a workflow."),
    ("gh-cache-2", "d1", "CI/CD – GitHub Actions", "reference", "Caching dependencies",
     "The cache key should hash package-lock.json so npm dependencies refresh when it changes."),
    ("gh-cache-3", "d1", "CI/CD – GitHub Actions", "reference", "Caching dependencies",
     "Cache npm dependencies: restore keys let a workflow fall back to an older npm cache."),
    ("ecs-trouble", "d2", "Troubleshooting", "troubleshooting", "ECS task stopped",
     "If an ECS task stopped with CannotPullContainerError, check the ECR image and network."),
    ("k8s-hpa", "d3", "Deploy targets (stretch)", "reference", "Horizontal Pod Autoscaling",
     "A HorizontalPodAutoscaler scales Kubernetes pods based on CPU utilization."),
]


@pytest.fixture
def retriever(tmp_path):
    emb = FakeEmbedder()
    rows = [{"chunk_id": cid, "doc_id": doc, "source_id": "1", "category": cat, "doc_type": dt,
             "title": title, "url": f"https://example.com/{cid}", "heading_path": title,
             "chunk_index": 0, "content_hash": cid, "text": text, "vector": emb.embed(text)}
            for cid, doc, cat, dt, title, text in CORPUS]
    table = lancedb.connect(str(tmp_path)).create_table("chunks", data=rows)
    table.create_fts_index("text")
    return LocalLanceRetriever(embedder=emb, cfg=replace(CONFIG, max_per_doc=2), vectors_dir=tmp_path)


@pytest.fixture
def versions(tmp_path):
    p = tmp_path / "versions.json"
    p.write_text("""{
      "actions/setup-node": {"tool": "actions/setup-node", "latest": "v6.0.0", "released_at": "2026-09-01T00:00:00Z",
                             "extra": {"pin_hint": "actions/setup-node@v6"}},
      "actions/cache": {"tool": "actions/cache", "latest": "v6.1.0", "extra": {"pin_hint": "actions/cache@v6"}},
      "nodejs": {"tool": "nodejs", "latest": "26.11.1", "lts": "24.21.0", "extra": {"supported_cycles": ["26", "24"]}},
      "nodejs-v18": {"tool": "nodejs-v18", "latest": null, "eol": "2025-04-30", "extra": {"status": "EOL"}},
      "lambda-runtime-python3.13": {"tool": "lambda-runtime-python3.13", "latest": null,
                                    "extra": {"label": "Python 3.13", "status": "supported"}},
      "lambda-runtime-python3.8": {"tool": "lambda-runtime-python3.8", "latest": null,
                                   "extra": {"label": "Python 3.8", "status": "deprecated"}},
      "kubernetes": {"tool": "kubernetes", "latest": "1.37.1", "extra": {"supported_cycles": ["1.37"]}}
    }""")
    return VersionRegistry(p, today=date(2026, 10, 12))


# ---- retriever -------------------------------------------------------------------

def test_retrieves_relevant_chunk_first(retriever):
    hits = retriever.retrieve("why did my ECS task stop with CannotPullContainerError", k=3)
    assert hits[0].chunk_id == "ecs-trouble"
    assert 0 < hits[0].score <= 1


def test_category_filter_excludes_other_categories(retriever):
    hits = retriever.retrieve("npm cache workflow", filters={"category": "Troubleshooting"}, k=5)
    assert {h.metadata["category"] for h in hits} == {"Troubleshooting"}


def test_max_per_doc_diversifies(retriever):
    hits = retriever.retrieve("cache npm dependencies workflow", k=5)
    assert sum(h.metadata["doc_id"] == "d1" for h in hits) == 2


def test_unknown_filter_field_rejected():
    with pytest.raises(ValueError):
        check_filters({"url": "x"})


def test_filter_translation_escapes_and_combines():
    assert lance_where({"category": "O'Brien", "doc_type": ["a", "b"]}) == \
        "category IN ('O''Brien') AND doc_type IN ('a', 'b')"
    assert kb_filter({"category": "x"}) == {"equals": {"key": "category", "value": "x"}}
    assert kb_filter({"category": "x", "doc_type": ["a", "b"]}) == {"andAll": [
        {"equals": {"key": "category", "value": "x"}}, {"in": {"key": "doc_type", "value": ["a", "b"]}}]}


def test_diversify_keeps_given_order():
    # Input order is the ranking (fused for hybrid search), so diversify must not re-sort.
    cs = [RetrievedChunk(chunk_id=str(i), text="", url="u", title="t", score=s, metadata={"doc_id": d})
          for i, (s, d) in enumerate([(0.5, "a"), (0.9, "a"), (0.7, "b"), (0.8, "a")])]
    assert [c.chunk_id for c in diversify(cs, k=3, max_per_doc=2)] == ["0", "1", "2"]


class NoiseEmbedder:
    """Meaningless but deterministic vectors: vector search ranks chunks arbitrarily."""

    def embed(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode()).digest()
        v = [b - 127.5 for b in (h * 4)[:DIMS]]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]


def _table(tmp_path, emb, fts=True):
    rows = [{"chunk_id": cid, "doc_id": doc, "source_id": "1", "category": cat, "doc_type": dt,
             "title": title, "url": f"https://example.com/{cid}", "heading_path": title,
             "chunk_index": 0, "content_hash": cid, "text": text, "vector": emb.embed(text)}
            for cid, doc, cat, dt, title, text in CORPUS]
    table = lancedb.connect(str(tmp_path)).create_table("chunks", data=rows)
    if fts:
        table.create_fts_index("text")


def test_keyword_search_finds_exact_term_vector_search_misses(tmp_path):
    emb = NoiseEmbedder()
    _table(tmp_path, emb)
    cfg = replace(CONFIG, max_per_doc=5)
    hybrid = LocalLanceRetriever(embedder=emb, cfg=cfg, vectors_dir=tmp_path, hybrid=True)
    top = hybrid.retrieve("CannotPullContainerError", k=3)[0]
    assert top.chunk_id == "ecs-trouble" and "keyword" in top.metadata["match"]
    # The score stays the meaning (cosine) score, so the min_score cut-off still means the same.
    assert top.score == round(sum(a * b for a, b in zip(emb.embed("CannotPullContainerError"),
                                                         emb.embed(CORPUS[3][5]))), 4)


def test_keyword_query_strips_query_syntax():
    from infrapilot_rag.retriever import keyword_query

    assert keyword_query('What does "Fn::Sub" do?') == "What does Fn Sub do"


def test_missing_text_index_falls_back_to_vector(tmp_path):
    _table(tmp_path, FakeEmbedder(), fts=False)
    r = LocalLanceRetriever(embedder=FakeEmbedder(), vectors_dir=tmp_path, hybrid=True)
    assert r.hybrid is False and r.retrieve("ECS task stopped", k=2)


# ---- versions --------------------------------------------------------------------

def test_versions_picked_by_topic_and_name(versions):
    tools = {f["tool"] for f in versions.relevant("How do I set up setup-node with npm caching?")}
    assert {"actions/setup-node", "nodejs"} <= tools
    assert "nodejs-v18" not in tools  # end of life
    assert "kubernetes" not in tools


def test_lambda_runtimes_only_supported_unless_named(versions):
    tools = {f["tool"] for f in versions.relevant("Which Python runtime should my Lambda use?")}
    assert tools == {"lambda-runtime-python3.13"}
    named = {f["tool"] for f in versions.relevant("Is python3.8 still OK for Lambda?")}
    assert "lambda-runtime-python3.8" in named


def test_plain_word_does_not_trigger_action(versions):
    assert versions.relevant("How do I cache CloudFront responses?") == []


# ---- prompt ----------------------------------------------------------------------

def test_prompt_snapshot(versions):
    chunks = [RetrievedChunk(chunk_id="c1", text="Document: Caching\n\nUse actions/cache.",
                             url="https://example.com/c1", title="Caching", score=0.8)]
    facts = versions.relevant("cache npm in a GitHub Actions workflow with setup-node")
    rendered = SYSTEM_PROMPT + "\n\n=====\n\n" + build_user_message("How do I cache npm?", chunks, facts)
    snap = SNAPSHOTS / "prompt.txt"
    if os.environ.get("UPDATE_SNAPSHOTS") or not snap.exists():
        snap.write_text(rendered)
    assert rendered == snap.read_text(), "prompt changed: review, then rerun with UPDATE_SNAPSHOTS=1"


def test_parse_citations():
    valid, invalid, v = parse_citations("Use cache [2]. Also [1][2] and [7]. Pin @v6 [V].", n_chunks=3)
    assert valid == [2, 1] and invalid == [7] and v is True


# ---- answer service --------------------------------------------------------------

def test_answer_maps_citations(retriever, versions):
    llm = FakeLLM("Use `actions/setup-node@v6` with `cache: npm` [1][V].")
    ans = AnswerService(retriever, llm, versions).ask("cache npm dependencies in a workflow")
    assert not ans.refused
    assert ans.citations[0].chunk_id == ans.chunks[0].chunk_id
    assert ans.cited_versions and "actions/setup-node" in ans.versions_used
    assert "<version_facts>" in llm.calls[0]


def test_refuses_without_llm_call_when_nothing_relevant(retriever, versions):
    llm = FakeLLM("should not be called")
    cfg = replace(CONFIG, min_score=0.99)
    ans = AnswerService(retriever, llm, versions, cfg).ask("What is the capital of France?")
    assert ans.refused and ans.text == REFUSAL and llm.calls == []


def test_model_refusal_detected(retriever, versions):
    ans = AnswerService(retriever, FakeLLM(REFUSAL), versions).ask("ECS task stopped error")
    assert ans.refused and ans.citations == []


# ---- model selection -------------------------------------------------------------

def test_model_routing(monkeypatch):
    from infrapilot_rag.llm import BedrockChat, OpenAIChat, make_chat

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(make_chat("gpt-5-mini"), OpenAIChat)
    assert make_chat("gpt-5-mini").reasoning and not make_chat("gpt-4.1-mini").reasoning
    assert isinstance(make_chat("nova-pro"), BedrockChat)


def test_missing_openai_key_is_a_clear_error(monkeypatch):
    from infrapilot_rag.llm import make_chat

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        make_chat("gpt-5-mini")


def test_dotenv_strips_inline_comments(tmp_path, monkeypatch):
    from infrapilot_rag.config import _load_dotenv

    env = tmp_path / ".env"
    env.write_text('A_MODEL=gpt-5-mini   # or gpt-5\nA_QUOTED="x # y"\nexport A_PLAIN=v\n')
    for k in ("A_MODEL", "A_QUOTED", "A_PLAIN"):
        monkeypatch.delenv(k, raising=False)
    _load_dotenv(env)
    import os
    assert (os.environ["A_MODEL"], os.environ["A_QUOTED"], os.environ["A_PLAIN"]) == ("gpt-5-mini", "x # y", "v")


def test_versions_added_for_actions_pinned_in_excerpts(versions):
    q = "How do I run the same job against several Node versions?"
    assert "actions/setup-node" not in {f["tool"] for f in versions.relevant(q)}
    ctx = "steps:\n  - uses: actions/setup-node@v4\n"
    assert "actions/setup-node" in {f["tool"] for f in versions.relevant(q, ctx)}
