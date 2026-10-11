"""Offline unit tests for registry, cleaning, chunking, and dedup."""

from datetime import datetime, timezone

from infrapilot_data import registry
from infrapilot_data.chunk import chunk_document
from infrapilot_data.config import ChunkingConfig
from infrapilot_data.dedup import dedupe, hamming, simhash
from infrapilot_data.fetchers import STRATEGIES
from infrapilot_data.fetchers.base import canonical_url, markdown_variant
from infrapilot_data.models import NormalizedDoc
from infrapilot_data.normalize import clean_markdown, quality_issue, strip_boilerplate
from infrapilot_data.text import count_tokens, sha256


def make_doc(body: str, doc_id="d1", source_id="1", url="https://example.com/a") -> NormalizedDoc:
    return NormalizedDoc(
        doc_id=doc_id, source_id=source_id, category="Test", doc_type="reference", title="Doc",
        url=url, fetched_at=datetime(2026, 10, 9, tzinfo=timezone.utc), content_hash=sha256(body),
        token_count=count_tokens(body), body_markdown=body,
    )


# ---- registry -------------------------------------------------------------------

def test_every_source_has_a_known_strategy():
    specs = registry.load_sources()
    assert len(specs) >= 59
    for s in specs:
        assert s.strategy == "skip" or s.strategy in STRATEGIES, s.id
        if s.strategy == "skip":
            assert s.skip_reason, f"#{s.id} is skipped without a reason"


def test_source_ids_are_unique():
    ids = [s.id for s in registry.load_sources()]
    assert len(ids) == len(set(ids))


# ---- URLs -----------------------------------------------------------------------

def test_markdown_variants():
    assert markdown_variant("https://docs.aws.amazon.com/AmazonECS/latest/developerguide/x.html") \
        == "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/x.md"
    assert markdown_variant("https://docs.docker.com/reference/dockerfile/") \
        == "https://docs.docker.com/reference/dockerfile.md"
    assert markdown_variant("https://github.com/rhysd/actionlint/blob/main/docs/checks.md") \
        == "https://raw.githubusercontent.com/rhysd/actionlint/main/docs/checks.md"
    assert markdown_variant("https://kubernetes.io/docs/concepts/") is None


def test_canonical_url_drops_fragment_and_index():
    assert canonical_url("https://A.com/docs/index.html#x") == "https://a.com/docs/"


# ---- cleaning -------------------------------------------------------------------

def test_clean_markdown_strips_noise_but_keeps_code_and_expressions():
    raw = (
        "---\ntitle: x\n---\n# Title\n<a name=\"t\"></a>\n"
        "See [the guide](guide.md) and ![img](a.png).<!-- hidden -->\n\n"
        "{% data reusables.foo %}\n\n"
        "```yaml\nrun: echo ${{ github.sha }} [keep](this.md)\n```\n"
    )
    out = clean_markdown(raw)
    assert "title: x" not in out
    assert "<a name" not in out
    assert "See the guide and ." in out
    assert "hidden" not in out and "reusables" not in out
    assert "${{ github.sha }} [keep](this.md)" in out  # code blocks untouched


def test_boilerplate_lines_removed_across_source():
    banner = "This is the new CloudFormation Template Reference Guide. Please update your bookmarks."
    docs = [make_doc(f"# Doc {i}\n\n{banner}\n\nUnique content number {i} " + "word " * 50,
                     doc_id=f"d{i}", url=f"https://e.com/{i}") for i in range(6)]
    cleaned, removed = strip_boilerplate(docs)
    assert banner in removed
    assert all(banner not in d.body_markdown for d in cleaned)


def test_navigation_pages_are_dropped():
    nav = "# Topics\n\n" + "\n".join(f"+ Page {i}" for i in range(40))
    assert quality_issue(make_doc(nav)) == "navigation_page"
    assert quality_issue(make_doc("# Short\n\nhi")) == "too_short"
    assert quality_issue(make_doc("# Real\n\n" + "A real paragraph with content. " * 30)) is None


# ---- chunking -------------------------------------------------------------------

def test_chunks_respect_headings_code_and_limits():
    code = "```yaml\n" + "\n".join(f"step{i}: run something {i}" for i in range(30)) + "\n```"
    body = (
        "# Workflow syntax\n\nIntro paragraph. " + "Words here. " * 40 + "\n\n"
        "## jobs.<job_id>.steps\n\n" + "Steps text. " * 60 + "\n\n" + code + "\n\n"
        "## on\n\n" + "Trigger text. " * 80
    )
    cfg = ChunkingConfig(target_tokens=200, max_tokens=320, min_tokens=20, overlap_tokens=30)
    chunks = chunk_document(make_doc(body), cfg)
    assert len(chunks) > 3
    for c in chunks:
        assert c.text.startswith("Document: Doc")
        assert c.text.count("```") % 2 == 0, "code fence split mid-block"
        assert c.token_count <= cfg.max_tokens + 40  # header allowance
    assert any(c.heading_path[-1:] == ["on"] for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_oversized_code_block_is_split_with_fences_reopened():
    code = "```json\n" + "\n".join(f'"key{i}": "value number {i} with padding"' for i in range(400)) + "\n```"
    cfg = ChunkingConfig(target_tokens=200, max_tokens=300, min_tokens=20, overlap_tokens=0)
    chunks = chunk_document(make_doc("# Big\n\n" + code), cfg)
    assert len(chunks) > 5
    assert all(c.text.count("```") == 2 for c in chunks)


# ---- dedup ----------------------------------------------------------------------

def test_simhash_near_duplicates():
    a = "The quick brown fox jumps over the lazy dog. " * 40
    b = a + " One extra sentence at the end."
    c = "Completely different content about Kubernetes autoscaling and pods. " * 40
    assert hamming(simhash(a), simhash(b)) <= 3
    assert hamming(simhash(a), simhash(c)) > 10


def test_dedupe_prefers_earlier_source():
    text = "# Same\n\n" + "Identical body text for both docs. " * 30
    primary = make_doc(text, doc_id="p", source_id="2", url="https://a.com/x")
    mirror = make_doc(text, doc_id="m", source_id="9", url="https://b.com/x")
    kept, dropped = dedupe([mirror, primary], {"2": 0, "9": 1})
    assert [d.doc_id for d in kept] == ["p"]
    assert dropped == [("m", "p", "exact")]


def test_long_tables_repeat_header_and_bare_headings_merge_forward():
    rows = "\n".join(f"| CKV_DOCKER_{i} | Ensure rule number {i} is satisfied in the Dockerfile |" for i in range(200))
    body = "# Checkov rules\n\n## Dockerfile scans\n\n| Id | Policy |\n|---|---|\n" + rows
    cfg = ChunkingConfig(target_tokens=200, max_tokens=300, min_tokens=40, overlap_tokens=0)
    chunks = chunk_document(make_doc(body), cfg)
    assert len(chunks) > 3
    assert all("| Id | Policy |" in c.text for c in chunks)
    assert all(c.token_count >= 40 for c in chunks), "heading-only chunk survived"
