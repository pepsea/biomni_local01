"""index.html の整合性チェック（ブラウザ不要）.

実際に踏んだバグ:
  履歴タブを足したとき、ボタンとパネルは追加したのに showTab() の中の
  ["answer","hyp","sources","trace"] に "history" を書き足し忘れた。
  ボタンは反応する（aria-pressed は変わる）のに中身が出ない、という
  もっとも気づきにくい壊れ方をした。

タブは今後も増える。列挙を 2 か所に持たせない、を機械的に守らせる。
"""

import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "backend/app/static/index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _tab_buttons(html: str) -> set[str]:
    return set(re.findall(r'data-tab="([\w-]+)"', html))


def _tab_pages(html: str) -> set[str]:
    return set(re.findall(r'class="tabpage[^"]*"\s+id="tab-([\w-]+)"', html))


def test_every_tab_button_has_a_page(html):
    missing = _tab_buttons(html) - _tab_pages(html)
    assert not missing, f"ボタンはあるがパネルが無いタブ: {sorted(missing)}"


def test_every_page_has_a_tab_button(html):
    orphan = _tab_pages(html) - _tab_buttons(html)
    assert not orphan, f"パネルはあるがボタンが無いタブ: {sorted(orphan)}"


def test_the_history_tab_exists(html):
    assert "history" in _tab_buttons(html)
    assert "history" in _tab_pages(html)


def test_show_tab_does_not_hardcode_the_tab_list(html):
    """showTab がタブ名を列挙していないこと。

    列挙すると、タブを増やしたときに書き足し忘れて
    「ボタンは押せるのに中身が出ない」状態になる。
    """
    body = re.search(r"function showTab\(name\) \{(.*?)\n\}", html, re.DOTALL)
    assert body, "showTab() が見つかりません"
    src = body.group(1)
    for name in _tab_buttons(html):
        assert f'"{name}"' not in src, (
            f'showTab() がタブ名 "{name}" を直書きしています。'
            "document.querySelectorAll('.tabpage') から引いてください"
        )
    assert ".tabpage" in src, "showTab() は .tabpage を走査して切り替えること"


def test_history_controls_are_present(html):
    """検索欄とファセットが揃っているか（loadHistory が参照する id）。"""
    for element_id in ("hq", "hprovider", "hmodel", "hmode", "horganism", "hstatus",
                       "hclear", "hresult"):
        assert f'id="{element_id}"' in html, f"#{element_id} がありません"


def test_history_filters_match_the_selects(html):
    """H_FILTERS と実際の <select> がずれていないこと。"""
    declared = re.search(r"const H_FILTERS = \[(.*?)\]", html)
    assert declared
    names = re.findall(r'"(\w+)"', declared.group(1))
    for n in names:
        assert f'id="h{n}"' in html, f"H_FILTERS に {n} があるが #h{n} が無い"


def test_favicon_is_inlined(html):
    """外部リクエストを増やさず、404 もコンソールに出さない。"""
    assert 'rel="icon"' in html
    assert "data:image/svg+xml" in html
