"""テストを環境から切り離す.

biomni_hypo.config は import 時に .env を読む（そうしないと非 Docker 実行で
.env が効かない）。ただしテストが開発者の .env に左右されるのは困るので、
ここで読み込みを止め、関連する環境変数も落としておく。

conftest.py はテストモジュールより先に import されるので、
biomni_hypo.config の import 時点ではこの設定が効いている。
"""

import os

import pytest

# biomni_hypo を import する前に立てること
os.environ["HYPO_SKIP_DOTENV"] = "1"

#: テスト結果を揺らしうる環境変数。プロセスに残っていたら落とす
_STRAY_PREFIXES = ("HYPO_", "BIOMNI_", "OLLAMA_", "LLM_", "APP_", "COMPOSE_")
_STRAY_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY")

def _drop_stray(env: "os._Environ[str] | dict[str, str]") -> None:
    for key in list(env):
        if key == "HYPO_SKIP_DOTENV":
            continue
        if key.startswith(_STRAY_PREFIXES) or key in _STRAY_KEYS:
            env.pop(key, None)


_drop_stray(os.environ)


@pytest.fixture(scope="session", autouse=True)
def _neutralise_biomni_dotenv():
    """biomni を先に import して .env の読み込みを済ませ、その汚染を落とす。

    biomni/agent/a1.py は import 時に `load_dotenv(".env", override=False)` を
    実行する。モジュールスコープの fixture（統合テストの `traced` など）は
    関数スコープの掃除より先に走るので、セッション開始時点で済ませておく。
    """
    try:
        import biomni.agent  # noqa: F401
    except Exception:  # noqa: BLE001 - biomni が無い環境では何もしなくてよい
        pass
    _drop_stray(os.environ)


@pytest.fixture(autouse=True)
def _clean_environment():
    """各テストの前に、設定由来の環境変数を落とす。

    biomni は import 時に CWD の .env を python-dotenv で読み込む
    （biomni/agent/a1.py の `load_dotenv(".env", override=False)`）。
    さらに apply_biomni_env() も BIOMNI_* を書き込むので、テスト間で漏れる。

    monkeypatch は使わない。**テスト終了時に値を復元してしまう**ため、
    モジュールスコープの fixture（統合テストの `traced` など）が
    次に走るときに汚染が戻ってしまう。ここでは復元せず捨てる。
    """
    _drop_stray(os.environ)
