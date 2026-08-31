"""biomni に無い文献検索（Europe PMC）.

なぜ足したか:
  biomni の文献ツールは PubMed / arXiv / Google Scholar。後ろ 2 つは
  商用ポリシーで拒否（スクレイピング）か、生物医学を収載していない。
  残るのは PubMed だけで、これでは「独立した情報源を複数当たる」（§3）が
  文献について成り立たない。
"""

from unittest.mock import MagicMock, patch

import pytest

from biomni_hypo.extra_tools import EXTRA_SCHEMAS, MODULE_NAME, query_europepmc

SAMPLE = {
    "resultList": {
        "result": [
            {
                "title": "FGFR1 signalling in bone remodelling.",
                "pmid": "37821999",
                "pmcid": "PMC10592456",
                "doi": "10.1371/journal.pone.0291567",
                "journalTitle": "PLoS One",
                "pubYear": "2023",
                "abstractText": "FGFR1 is required for osteoblast differentiation. " * 40,
            },
            {"title": "A preprint without a PMID.", "pmcid": "PMC9999999",
             "abstractText": "Short abstract."},
        ]
    }
}


def _response(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_identifiers_appear_in_the_text():
    """識別子が本文に現れないと、根拠として検証できない（§3）。"""
    with patch("biomni_hypo.extra_tools.requests.get", return_value=_response(SAMPLE)):
        out = query_europepmc("FGFR1 AND osteoporosis")

    assert "PMID:37821999" in out
    assert "PMC10592456" in out
    assert "DOI:10.1371/journal.pone.0291567" in out
    assert "PLoS One 2023" in out


def test_a_preprint_without_a_pmid_still_carries_an_id():
    """PubMed に無い文献は PMID を持たない。PMCID が唯一の識別子になる。"""
    with patch("biomni_hypo.extra_tools.requests.get", return_value=_response(SAMPLE)):
        out = query_europepmc("x")
    assert "PMC9999999" in out


def test_long_abstracts_are_cut():
    """全文を入れると 1 回で文脈を食い潰す（§40）。"""
    with patch("biomni_hypo.extra_tools.requests.get", return_value=_response(SAMPLE)):
        out = query_europepmc("x")
    assert len(out) < 4000, len(out)
    assert "…" in out


def test_no_results_says_so():
    with patch("biomni_hypo.extra_tools.requests.get",
               return_value=_response({"resultList": {"result": []}})):
        assert "No results" in query_europepmc("なにか")


def test_a_network_failure_is_returned_as_an_observation():
    """例外を投げずに、観測として返すこと（エージェントが読んで次を決める）。"""
    with patch("biomni_hypo.extra_tools.requests.get", side_effect=TimeoutError("timed out")):
        out = query_europepmc("x")
    assert out.startswith("Error: Europe PMC query failed")
    assert "TimeoutError" in out


def test_the_page_size_is_bounded():
    """モデルが max_papers=1000 と書いても、際限なく取りにいかないこと。"""
    with patch("biomni_hypo.extra_tools.requests.get", return_value=_response(SAMPLE)) as get:
        query_europepmc("x", max_papers=1000)
    assert get.call_args.kwargs["params"]["pageSize"] == 25


def test_the_schema_matches_the_signature():
    """スキーマと実物がずれていると、モデルは通らない呼び方を書く（§37）。"""
    import inspect

    schema = EXTRA_SCHEMAS[0]
    params = set(inspect.signature(query_europepmc).parameters)
    declared = {p["name"] for p in schema["required_parameters"]}
    declared |= {p["name"] for p in schema["optional_parameters"]}
    assert declared <= params, f"スキーマにしか無い引数: {declared - params}"
    assert "query" in declared, "必須の引数が宣言されていない"


def test_the_module_name_is_not_a_biomni_one():
    """biomni のモジュールと混ざらない名前にすること。"""
    assert MODULE_NAME.startswith("biomni_hypo.")


def test_the_policy_allows_it():
    from biomni_hypo.policy import ResourcePolicy

    policy = ResourcePolicy.load()
    assert policy.check_tool("query_europepmc").allowed
    # 一方、スクレイピング系は拒否のままであること
    assert not policy.check_tool("query_scholar").allowed
    assert not policy.check_tool("search_google").allowed


@pytest.mark.parametrize("name", ["query_scholar", "search_google"])
def test_denied_literature_tools_have_a_stated_reason(name):
    from biomni_hypo.policy import ResourcePolicy

    decision = ResourcePolicy.load().check_tool(name)
    assert decision.reason, f"{name} を拒否する理由が書かれていない"
