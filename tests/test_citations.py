from biomni_hypo.citations import bare_identifier, extract_citations
from biomni_hypo.schemas import ResourceKind, ToolCall


def _ids(citations):
    return {c.identifier for c in citations}


def test_extracts_common_identifiers():
    text = (
        "rs2981582 maps to FGFR2 (ENSG00000138675). See PMID: 17529967 and "
        "doi:10.1038/ng.2007.53. Pathway R-HSA-109582, term GO:0006915, GSE12345."
    )
    ids = _ids(extract_citations(text, step_idx=2))
    assert "PMID:17529967" in ids
    assert "DOI:10.1038/ng.2007.53" in ids
    assert "ENSG00000138675" in ids
    assert "rs2981582" in ids
    assert "R-HSA-109582" in ids
    assert "GO:0006915" in ids
    assert "GSE12345" in ids


def test_pubmed_url_form_is_recognised():
    ids = _ids(extract_citations("see https://pubmed.ncbi.nlm.nih.gov/31234567/"))
    assert ids == {"PMID:31234567"}


def test_excerpt_comes_from_real_text_not_the_model():
    text = "prefix " * 40 + "PMID: 17529967 tail marker"
    (c,) = [x for x in extract_citations(text) if x.identifier == "PMID:17529967"]
    assert "tail marker" in c.excerpt
    assert c.excerpt.startswith("…")  # 前方が切られていることを示す


def test_gated_patterns_need_their_tool():
    text = "structure 6XYZ was resolved"
    assert not [c for c in extract_citations(text) if c.identifier == "6XYZ"]
    gated = extract_citations(text, tools_in_step=[ToolCall(name="query_pdb")])
    assert "6XYZ" in _ids(gated)


def test_uniprot_needs_its_tool_to_avoid_false_positives():
    # P04637 のような文字列は一般文にも現れうるので、ツール呼び出しでゲートする
    text = "the value P04637 appeared"
    assert not [c for c in extract_citations(text) if c.identifier == "P04637"]
    gated = extract_citations(text, tools_in_step=["query_uniprot"])
    assert "P04637" in _ids(gated)


def test_duplicates_are_collapsed():
    text = "PMID: 17529967 and again PMID:17529967"
    assert len([c for c in extract_citations(text) if c.kind == ResourceKind.LITERATURE]) == 1


def test_empty_text_is_safe():
    assert extract_citations("") == []


def test_bare_identifier_strips_prefix_but_keeps_go():
    assert bare_identifier("PMID:17529967") == "17529967"
    assert bare_identifier("GO:0006915") == "GO:0006915"
    assert bare_identifier("rs2981582") == "rs2981582"
