""".env の読み込み.

これが無いと、ユーザーが .env を編集しても非 Docker 実行では一切効かない。
Docker では Compose が環境変数を注入するため気付けなかった。
"""

import os

from biomni_hypo.config import Settings, load_dotenv_file


def test_loads_keys_from_the_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("HYPO_PROVIDER=anthropic\nHYPO_MODEL=claude-opus-5\n", encoding="utf-8")
    monkeypatch.delenv("HYPO_PROVIDER", raising=False)
    monkeypatch.delenv("HYPO_MODEL", raising=False)

    applied = load_dotenv_file(env)

    assert applied == {"HYPO_PROVIDER": "anthropic", "HYPO_MODEL": "claude-opus-5"}
    assert Settings().provider == "anthropic"
    assert Settings().model == "claude-opus-5"


def test_existing_environment_wins(tmp_path, monkeypatch):
    """Compose や systemd が渡した値が .env より強いこと。"""
    env = tmp_path / ".env"
    env.write_text("HYPO_MODEL=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("HYPO_MODEL", "from-environment")

    applied = load_dotenv_file(env)

    assert applied == {}
    assert os.environ["HYPO_MODEL"] == "from-environment"


def test_ignores_comments_and_blank_lines(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "\n# コメント\n  # 字下げコメント\nHYPO_MODEL=x\n\n不正な行\n", encoding="utf-8"
    )
    monkeypatch.delenv("HYPO_MODEL", raising=False)
    assert load_dotenv_file(env) == {"HYPO_MODEL": "x"}


def test_strips_quotes_and_export_prefix(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('export HYPO_MODEL="claude-opus-5"\nAPP_BIND=\'127.0.0.1\'\n', encoding="utf-8")
    monkeypatch.delenv("HYPO_MODEL", raising=False)
    monkeypatch.delenv("APP_BIND", raising=False)
    assert load_dotenv_file(env) == {"HYPO_MODEL": "claude-opus-5", "APP_BIND": "127.0.0.1"}


def test_value_may_contain_equals(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OLLAMA_BASE_URL=http://host:11434/?a=1\n", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert load_dotenv_file(env)["OLLAMA_BASE_URL"] == "http://host:11434/?a=1"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv_file(tmp_path / "nope.env") == {}
