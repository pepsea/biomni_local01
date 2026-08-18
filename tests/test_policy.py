import pytest

from biomni_hypo.guard import PolicyGuard
from biomni_hypo.policy import ResourcePolicy


@pytest.fixture(scope="module")
def policy():
    return ResourcePolicy.load()


def test_policy_loads(policy):
    assert policy.version >= 1
    assert policy.mode == "commercial_only"
    assert policy.allowed_dataset_names()


def test_non_commercial_tool_is_denied(policy):
    d = policy.check_tool("query_kegg")
    assert not d.allowed
    assert "KEGG" in d.reason


def test_ordinary_tool_is_allowed(policy):
    assert policy.check_tool("query_opentarget").allowed


def test_review_required_tool_is_allowed_but_flagged(policy):
    d = policy.check_tool("query_cbioportal")
    assert d.allowed and d.review_required


def test_dataset_allowlist_is_default_deny(policy):
    assert policy.check_dataset("gwas_catalog.pkl").allowed
    assert not policy.check_dataset("omim.parquet").allowed
    assert not policy.check_dataset("whatever_random.csv").allowed


def test_copyleft_dataset_is_flagged_for_review(policy):
    d = policy.check_dataset("proteinatlas.tsv")
    assert d.allowed and d.review_required
    assert d.license == "CC-BY-SA-3.0"


def test_model_allowlist(policy):
    assert policy.check_model("qwen3:14b").allowed
    assert not policy.check_model("llama3.1:8b").allowed
    assert not policy.check_model("gemma3:12b").allowed


def test_inspect_code_flags_denied_tool(policy):
    code = "from biomni.tool.database import query_kegg\nquery_kegg('hsa04110')\n"
    violations = policy.inspect_code(code)
    assert [v.name for v in violations].count("query_kegg") >= 1
    assert violations[0].line == 1


def test_inspect_code_flags_non_commercial_dataset(policy):
    code = "df = pd.read_parquet('msigdb_human_h_hallmark_geneset.parquet')"
    violations = policy.inspect_code(code)
    assert violations and violations[0].kind == "dataset"


def test_inspect_code_ignores_comments(policy):
    assert policy.inspect_code("# query_kegg is not allowed here") == []


def test_inspect_code_allows_clean_code(policy):
    code = "import pandas as pd\ndf = pd.read_pickle('gwas_catalog.pkl')\nprint(df.head())"
    assert policy.inspect_code(code) == []


def test_inspect_code_ignores_unrelated_user_files(policy):
    # ユーザーがアップロードした作業ファイルはデータレイクの許可リストと無関係
    assert policy.inspect_code("pd.read_csv('my_rnaseq_deg.csv')") == []


def test_guard_blocks_execution_and_records_it(policy):
    guard = PolicyGuard(policy)
    wrapped = guard.wrap(lambda code: "EXECUTED")

    assert wrapped("print('hello')") == "EXECUTED"

    out = wrapped("from biomni.tool.database import query_kegg")
    assert out.startswith("POLICY BLOCKED")
    assert "KEGG" in out
    assert len(guard.blocked) == 1
    assert guard.take_blocked() and guard.blocked == []
