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

STATIC = Path(__file__).resolve().parents[1] / "backend/app/static"
INDEX = STATIC / "index.html"
HISTORY = STATIC / "history.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def history_html() -> str:
    return HISTORY.read_text(encoding="utf-8")


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


def test_history_is_not_a_tab_anymore(html):
    """履歴はタブの 5 枚目ではなく独立ページ。

    入力欄と結果で画面が埋まるので、タブに置くと「そこにある」と気付けない。
    """
    assert "history" not in _tab_buttons(html)
    assert 'href="/history"' in html, "本体ページから履歴への導線が無い"


def test_the_main_page_can_open_a_run_by_url(html):
    """履歴ページは /?run=<id> で本体に飛ばす。受け側があること。"""
    assert "openRunFromUrl" in html
    assert 'get("run")' in html


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


def test_history_controls_are_present(history_html):
    """検索欄とファセットが揃っているか（loadHistory が参照する id）。"""
    for element_id in ("hq", "hprovider", "hmodel", "hmode", "horganism", "hstatus",
                       "hclear", "hresult"):
        assert f'id="{element_id}"' in history_html, f"#{element_id} がありません"


def test_history_page_links_back(history_html):
    assert 'href="/"' in history_html, "本体ページに戻る導線が無い"


def test_history_page_is_self_contained(history_html):
    """外部リソースを読まないこと（オフラインでも開ける）。"""
    for bad in ("http://", "https://cdn", "<script src="):
        assert bad not in history_html.replace(
            "http://www.w3.org/2000/svg", ""
        ), f"外部参照 {bad} がある"


def test_history_filters_match_the_selects(history_html):
    """H_FILTERS と実際の <select> がずれていないこと。"""
    declared = re.search(r"const H_FILTERS = \[(.*?)\]", history_html)
    assert declared
    names = re.findall(r'"(\w+)"', declared.group(1))
    for n in names:
        assert f'id="h{n}"' in history_html, f"H_FILTERS に {n} があるが #h{n} が無い"


@pytest.mark.parametrize("page", ["index.html", "history.html"])
def test_favicon_is_inlined(page):
    """外部リクエストを増やさず、404 もコンソールに出さない。"""
    text = (STATIC / page).read_text(encoding="utf-8")
    assert 'rel="icon"' in text
    assert "data:image/svg+xml" in text


def test_unusable_models_are_explained(html):
    """「接続済みなのに選べない」を黙って見せない。

    到達できているのに商用ポリシーで全部弾かれる状態がある。
    空の選択欄だけ出すと、利用者は何が起きたか分からない。
    """
    assert "使えるモデルが 1 つもありません" in html
    assert "商用利用ポリシーで使えません" in html
    assert "ollama pull qwen3:14b" in html, "次の一手が書かれていない"


def test_the_header_shows_how_many_models_are_usable(html):
    """「接続済み」だけでは足りない（0 件でも接続済みと出てしまう）。"""
    assert "使えるモデル" in html
    assert "接続済みだが使えるモデルなし" in html


def test_the_header_shows_the_build(html):
    """再ビルドし忘れを画面から見分けられるようにする。"""
    assert "health.build" in html


def test_an_empty_ollama_is_explained_separately(html):
    """モデル 0 件と、ポリシーで全部弾かれた、は原因も対処も違う。"""
    assert "モデルが 1 件もありません" in html
    assert "別の Ollama を見ています" in html
    assert "make model-check" in html
