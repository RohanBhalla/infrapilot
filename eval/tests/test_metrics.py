from infrapilot_eval import metrics as M


def test_ranks_ignores_fragments_and_slashes():
    got = ["https://a.com/x", "https://github.com/o/r#readme", "https://b.com/y/"]
    assert M.ranks(got, ["https://github.com/o/r"]) == 2
    assert M.ranks(got, ["https://b.com/y"]) == 3
    assert M.ranks(got, ["https://c.com"]) is None


def test_fact_recall_is_case_and_hyphen_insensitive():
    assert M.fact_recall("Grant least-privilege access with `Fn::Sub`", ["least privilege", "fn::sub"]) == 1.0
    assert M.fact_recall("nothing here", ["Retain", "Export"]) == 0.0
    assert M.fact_recall("x", []) is None


def test_citation_coverage_skips_code_and_lead_ins():
    text = "Set the cache input to npm in setup-node [1].\nHere is an example workflow configuration:\n```yaml\nuncited: line here\n```\n" \
           "This sentence states a fact but has no citation."
    assert M.citation_coverage(text) == 0.5


def test_stale_pins():
    facts = {"actions/checkout": {"extra": {"major": "v7"}}}
    assert M.stale_pins("uses: actions/checkout@v4 and actions/checkout@v7", facts) == ["actions/checkout@v4 (current v7)"]


def test_expected_version_patterns():
    v, pat = M.expected_version({"lts": "24.21.0"}, "lts")
    assert v == "24.21.0" and pat.search("Use Node.js 24") and not pat.search("Node.js 240")
    _, pat = M.expected_version({"extra": {"major": "v7"}}, "major")
    assert pat.search("actions/checkout@v7") and not pat.search("@v71")
    _, pat = M.expected_version({"latest": "1.37-eks-4"}, "latest_minor")
    assert pat.search("EKS supports 1.37") and not pat.search("1.3")
    _, pat = M.expected_version({"tool": "lambda-runtime-nodejs24.x", "extra": {}}, "runtime_id")
    assert pat.search("choose nodejs24.x")
    _, pat = M.expected_version({"extra": {"status": "deprecated"}}, "status")
    assert pat.search("It is deprecated")


def test_percentile():
    assert M.percentile([100, 200, 300, 400], 50) == 250


def test_runtime_id_accepts_readable_names():
    _, pat = M.expected_version({"tool": "lambda-runtime-nodejs24.x", "extra": {}}, "runtime_id")
    assert pat.search("Choose Node.js 24.x") and pat.search("use nodejs24.x") and not pat.search("Node.js 22")
    _, pat = M.expected_version({"tool": "lambda-runtime-python3.14", "extra": {}}, "runtime_id")
    assert pat.search("Python 3.14") and not pat.search("Python 3.1 ")
