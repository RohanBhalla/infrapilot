"""Intent + clarification eval (plan, Feature 3.1): intent accuracy, slot extraction F1, and
whether the assistant asked when it needed to (and only then)."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from infrapilot_assistant.dialog import parse_value, step
from infrapilot_assistant.intents import INTENTS
from infrapilot_assistant.state import DialogState

from . import metrics as M

EVAL_DIR = Path(__file__).resolve().parents[1]
CASES = EVAL_DIR / "intent" / "utterances.jsonl"
REPORTS = EVAL_DIR / "reports"
TARGETS = {"intent_accuracy": 0.9, "slot_f1": 0.85, "action_accuracy": 0.85}


@dataclass
class _Msg:
    role: str
    content: str


def load_cases(ids=None, limit=None) -> list[dict]:
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases[:limit] if limit else cases


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _norm(v) -> str:
    return str(v).strip().lower().strip("./").rstrip("/").replace(", ", ",")


def value_match(slot_type: str, expected, got) -> bool:
    if slot_type == "str":
        e, g = _norm(expected), _norm(got)
        if "," in e:  # expected lists like "lint, test" are unordered
            return set(e.split(",")) == set(g.split(","))
        return bool(e) and (e in g or g in e)
    return expected == got


def score_slots(intent_name: str, expected: dict, extracted: dict,
                known: dict | None = None) -> tuple[int, int, int]:
    """(true positives, false positives, false negatives) for one message. Values that only repeat
    what the session state already had (`known`) aren't new extractions, so they aren't scored."""
    intent = INTENTS.get(intent_name)
    known = known or {}
    # An accepted alternative intent may not have the expected intent's slots.
    expected = {k: v for k, v in expected.items() if intent and intent.slot(k)}
    got = {}
    for name, raw in extracted.items():
        slot = intent.slot(name) if intent else None
        if slot:
            v = parse_value(slot, raw)
            if v is not None and not (name in known and known[name] == v and name not in expected):
                got[name] = v
    tp = fp = 0
    for name, v in got.items():
        slot = intent.slot(name)
        if name in expected and value_match(slot.type, expected[name], v):
            tp += 1
        else:
            fp += 1
    fn = sum(1 for name in expected if not (name in got and value_match(
        intent.slot(name).type if intent and intent.slot(name) else "str", expected[name], got[name])))
    return tp, fp, fn


def evaluate_case(case: dict, classifier, versions) -> dict:
    state = DialogState(**case.get("state", {}))
    history = [_Msg(m["role"], m["content"]) for m in case.get("history", [])]
    exp = case["expect"]
    started = time.monotonic()
    try:
        c = classifier.classify(case["message"], state, history)
    except Exception as exc:
        return {"id": case["id"], "error": str(exc)[:200]}
    latency = int((time.monotonic() - started) * 1000)
    turn = step(case["message"], state, c, versions)
    intents = _as_list(exp["intent"])
    # Slots are only scored against the expected intent; with a wrong intent they all count as missed.
    known = case.get("state", {}).get("params", {})
    tp, fp, fn = score_slots(c.intent, exp.get("params", {}), c.params, known) if c.intent in intents else \
        (0, 0, len(exp.get("params", {})))
    asks = [q.slot for q in turn.questions]
    return {
        "id": case["id"], "message": case["message"][:200],
        "expected_intent": intents, "intent": c.intent, "confidence": c.confidence,
        "intent_ok": c.intent in intents,
        "expected_action": exp["action"], "action": turn.type, "action_ok": turn.type in exp["action"],
        "expected_asks": exp.get("asks"), "asks": asks,
        "asks_ok": None if exp.get("asks") is None or turn.type != "clarification"
        else set(asks) == set(exp["asks"]),
        "expected_params": exp.get("params", {}), "extracted": c.params,
        "tp": tp, "fp": fp, "fn": fn, "cost_usd": c.cost_usd, "latency_ms": latency,
        "reply": turn.message[:300],
    }


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    tp, fp, fn = (sum(r[k] for r in ok) for k in ("tp", "fp", "fn"))
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    lat = sorted(r["latency_ms"] for r in ok)
    return {
        "cases": len(rows), "errors": len(rows) - len(ok),
        "intent_accuracy": M.mean([r["intent_ok"] for r in ok]),
        "action_accuracy": M.mean([r["action_ok"] for r in ok]),
        "asks_accuracy": M.mean([r["asks_ok"] for r in ok if r["asks_ok"] is not None]),
        "slot_precision": p, "slot_recall": rc, "slot_f1": 2 * p * rc / (p + rc) if p + rc else 0.0,
        "latency_p50_ms": M.percentile(lat, 50), "latency_p95_ms": M.percentile(lat, 95),
        "cost_per_message_usd": M.mean([r["cost_usd"] for r in ok]),
        "total_cost_usd": sum(r["cost_usd"] for r in ok),
    }


def render(meta: dict, s: dict, rows: list[dict]) -> str:
    lines = [f"# Intent eval: {meta['date']} · {meta['model']}", "", f"{s['cases']} cases · {meta['seconds']} s", "",
             "| Metric | Value | Target |", "|---|---:|---:|"]
    for k, v in s.items():
        t = TARGETS.get(k)
        mark = "" if t is None or v is None else (" ✅" if v >= t else " ❌")
        val = "–" if v is None else (f"{v:.3f}" if isinstance(v, float) and v < 10 else f"{v:,.0f}" if isinstance(v, float) else v)
        lines.append(f"| {k} | {val}{mark} | {t if t else ''} |")
    lines += ["", "## Failures", "", "| Case | Problem |", "|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['id']} | error: {r['error']} |")
            continue
        why = []
        if not r["intent_ok"]:
            why.append(f"intent {r['intent']} (expected {'/'.join(r['expected_intent'])})")
        if not r["action_ok"]:
            why.append(f"action {r['action']} (expected {'/'.join(r['expected_action'])})")
        if r["asks_ok"] is False:
            why.append(f"asked {r['asks']} (expected {r['expected_asks']})")
        if r["fn"]:
            why.append(f"missed slots: expected {r['expected_params']}, got {r['extracted']}")
        if why:
            lines.append(f"| {r['id']} | {'; '.join(why)} |")
    return "\n".join(lines) + "\n"


def run(classifier, versions, model: str, ids=None, limit=None, workers=4, tag="") -> tuple[dict, Path]:
    cases = load_cases(ids, limit)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda c: evaluate_case(c, classifier, versions), cases))
    s = summarize(rows)
    meta = {"date": date.today().isoformat(), "model": model, "seconds": round(time.monotonic() - started, 1)}
    name = "-".join(x for x in [meta["date"], "intent", model, tag] if x)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{name}.json").write_text(json.dumps({"meta": meta, "summary": s, "rows": rows}, indent=2,
                                                     ensure_ascii=False, default=str))
    path = REPORTS / f"{name}.md"
    path.write_text(render(meta, s, rows))
    return s, path
