"""ローカルモデルの探索と選択."""

import pytest

from biomni_hypo.config import Settings
from biomni_hypo.mock_ollama import MockOllama
from biomni_hypo.models import (
    ModelNotAvailable,
    apply_model_selection,
    list_local_models,
    resolve_num_ctx,
)
from biomni_hypo.policy import ResourcePolicy, model_family

# (名前, サイズ, context長)
LOCAL = [
    ("qwen3:14b", 9_276_055_800, 40960),
    ("qwen3:8b-instruct-q4_K_M", 5_200_000_000, 40960),
    ("llama3.1:8b", 4_900_000_000, 131072),
    ("gemma3:12b", 8_100_000_000, 131072),
    ("deepseek-r1:7b", 4_700_000_000, 131072),
    ("weird-model:1b", 900_000_000, 8192),
]


@pytest.fixture(scope="module")
def policy():
    return ResourcePolicy.load()


@pytest.fixture
def catalog(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        yield list_local_models(s, policy)


# ------------------------------------------------------------ ファミリー判定


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("qwen3:14b", "qwen3"),
        ("qwen3:8b-instruct-q4_K_M", "qwen3"),
        ("QWEN3:4b", "qwen3"),
        ("library/gpt-oss:20b", "gpt-oss"),
        ("hf.co/user/Qwen3-14B-GGUF:Q4_K_M", "qwen3-14b-gguf"),
    ],
)
def test_model_family(name, expected):
    assert model_family(name) == expected


def test_tagged_variants_are_allowed(policy):
    """完全一致リストに無くても、ファミリーが同じなら許可する。"""
    d = policy.check_model("qwen3:8b-instruct-q4_K_M")
    assert d.allowed and d.license == "Apache-2.0" and d.matched_by == "family"


@pytest.mark.parametrize(
    ("name", "fragment"),
    [
        ("llama3.1:8b", "MAU"),
        ("gemma3:12b", "利用制限"),
        ("command-r:35b", "非商用"),
        ("codestral:22b", "非商用"),
        ("mistral-large:123b", "研究用途"),
        ("deepseek-coder-v2:16b", "用途制限"),
    ],
)
def test_denied_families(policy, name, fragment):
    d = policy.check_model(name)
    assert not d.allowed
    assert fragment in d.reason
    assert d.license != "unknown", "拒否理由と一緒にライセンス名も出すこと"


def test_deny_beats_allow(policy):
    """mistral は Apache-2.0 だが mistral-large は研究用途限定。拒否が勝つこと。"""
    assert policy.check_model("mistral-small:24b").allowed
    assert not policy.check_model("mistral-large:123b").allowed


def test_unknown_model_is_denied_by_default(policy):
    d = policy.check_model("something-nobody-knows:1b")
    assert not d.allowed and d.matched_by == "default"


# ------------------------------------------------------------ カタログ


def test_catalog_reads_local_models(catalog):
    assert catalog.reachable
    names = {m.name for m in catalog.models if m.installed}
    assert names == {n for n, _s, _c in LOCAL}


def test_selectable_excludes_denied_models(catalog):
    assert [m.name for m in catalog.selectable] == [
        "qwen3:14b",
        "qwen3:8b-instruct-q4_K_M",
        "deepseek-r1:7b",
    ]


def test_blocked_models_are_listed_with_reasons(catalog):
    """使えないモデルも理由付きで返す。黙って消すと「出てこない」と言われる。"""
    blocked = {m.name: m for m in catalog.blocked}
    assert set(blocked) == {"llama3.1:8b", "gemma3:12b", "weird-model:1b"}
    assert all(m.reason for m in blocked.values())


def test_not_installed_recommendations_are_included(catalog):
    not_installed = [m for m in catalog.models if not m.installed and m.local]
    assert "qwen3:32b" in {m.name for m in not_installed}
    # 未取得のローカルモデルは context 長を問い合わせない
    assert all(m.max_context == 0 for m in not_installed)


def test_metadata_is_populated(catalog):
    m = catalog.get("qwen3:14b")
    assert m.size_gb == pytest.approx(9.3)
    assert m.max_context == 40960
    assert m.quantization == "Q4_K_M"
    assert m.recommended


def test_context_length_is_only_fetched_for_allowed_models(catalog):
    assert catalog.get("llama3.1:8b").max_context == 0


def test_default_prefers_the_configured_model(catalog):
    assert catalog.default(preferred="deepseek-r1:7b").name == "deepseek-r1:7b"


def test_default_falls_back_to_largest_recommended(catalog):
    assert catalog.default(preferred="llama3.1:8b").name == "qwen3:14b"


def test_table_renders(catalog):
    table = catalog.as_table()
    assert "qwen3:14b" in table and "★" in table and "✕" in table


# ------------------------------------------------------------ num_ctx


def test_num_ctx_is_clamped_to_the_model_limit(catalog):
    resolved, note = resolve_num_ctx(catalog.get("qwen3:14b"), 65536)
    assert resolved == 40960
    assert "丸めました" in note


def test_num_ctx_under_the_limit_is_kept(catalog):
    resolved, note = resolve_num_ctx(catalog.get("qwen3:14b"), 32768)
    assert resolved == 32768 and note == ""


def test_warns_when_the_system_prompt_eats_the_context(catalog):
    _resolved, note = resolve_num_ctx(catalog.get("qwen3:14b"), 32768, prompt_tokens=30000)
    assert "残り" in note


# ------------------------------------------------------------ 選択の適用


def test_apply_selection_sets_model_and_clamps_num_ctx(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.num_ctx = 131072
        _catalog, notes = apply_model_selection(s, policy, model="qwen3:14b")
    assert s.model == "qwen3:14b"
    assert s.num_ctx == 40960
    assert any("丸めました" in n for n in notes)


def test_apply_selection_rejects_denied_model(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        with pytest.raises(ModelNotAvailable, match="ポリシー"):
            apply_model_selection(s, policy, model="llama3.1:8b")


def test_apply_selection_rejects_missing_model_with_a_pull_hint(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        with pytest.raises(ModelNotAvailable, match="ollama pull"):
            apply_model_selection(s, policy, model="qwen3:70b")


def test_non_strict_mode_falls_back_instead_of_raising(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        _catalog, notes = apply_model_selection(s, policy, model="llama3.1:8b", strict=False)
    assert s.model == "qwen3:14b"
    assert any("代わりに選びました" in n for n in notes)


def test_unreachable_ollama_is_reported(policy):
    s = Settings()
    s.ollama_base_url = "http://127.0.0.1:1"  # 誰もいないポート
    with pytest.raises(ModelNotAvailable, match="到達できません"):
        apply_model_selection(s, policy)


def test_no_usable_model_at_all(policy):
    with MockOllama(models=[("llama3.1:8b", 1, 8192)]) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        _catalog, notes = apply_model_selection(s, policy, strict=False)
    assert any("使用できるモデルが 1 つもありません" in n for n in notes)


# ------------------------------------------------------- クラウド（Claude API）


def test_cloud_models_appear_without_api_key_but_not_installed(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = ""
        catalog = list_local_models(s, policy, fetch_context_length=False)

    cloud = [m for m in catalog.models if not m.local]
    assert {m.name for m in cloud} >= {"claude-opus-5", "claude-sonnet-5"}
    assert all(not m.installed for m in cloud)
    assert all("ANTHROPIC_API_KEY" in m.reason for m in cloud)
    assert all(m.name not in {x.name for x in catalog.selectable} for m in cloud)


def test_cloud_models_become_selectable_with_api_key(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        catalog = list_local_models(s, policy, fetch_context_length=False)

    opus = catalog.get("claude-opus-5")
    assert opus.installed and opus.allowed and opus.recommended
    assert opus.provider == "anthropic" and not opus.local
    assert opus.max_context == 1_000_000
    assert opus.input_per_mtok == 5.0


def test_selecting_a_cloud_model_switches_provider_and_warns(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        _catalog, notes = apply_model_selection(s, policy, model="claude-opus-5")

    assert s.provider == "anthropic"
    assert s.model == "claude-opus-5"
    assert any("外部に送信" in n for n in notes), "外部送信の警告が出ていない"


def test_offline_mode_rejects_cloud_models(policy):
    """オフラインモードの約束（質問文を外部に出さない）を破らせない。"""
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        s.offline_mode = True
        with pytest.raises(ModelNotAvailable, match="オフラインモード"):
            apply_model_selection(s, policy, model="claude-opus-5")


def test_cloud_models_are_usable_when_ollama_is_down(policy):
    s = Settings()
    s.ollama_base_url = "http://127.0.0.1:1"
    s.anthropic_api_key = "sk-test"
    catalog = list_local_models(s, policy, fetch_context_length=False)
    assert not catalog.reachable
    assert [m.name for m in catalog.selectable] == [
        "claude-opus-5",
        "claude-haiku-4-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
    ]
    _catalog, notes = apply_model_selection(s, policy, catalog=catalog, strict=False)
    assert s.provider == "anthropic"


def test_num_ctx_is_not_applied_to_cloud_models(policy):
    """Claude は num_ctx を持たない。丸め処理を通さないこと。"""
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        s.num_ctx = 32768
        apply_model_selection(s, policy, model="claude-opus-5")
    assert s.num_ctx == 32768


def test_policy_knows_which_claude_models_reject_temperature(policy):
    """Claude 4.6 以降は temperature を送ると 400 になる。"""
    assert not policy.supports_temperature("anthropic", "claude-opus-5")
    assert not policy.supports_temperature("anthropic", "claude-sonnet-5")
    assert policy.supports_temperature("anthropic", "claude-haiku-4-5")
    assert policy.supports_temperature("ollama", "qwen3:14b")


# ---------------------------------------- 両方（Ollama + Claude）を選べる設定
# scripts/set-provider.sh both は ANTHROPIC_API_KEY を設定したまま
# HYPO_PROVIDER をどちらにも置ける。そのときの挙動をここで固定する。


def test_both_providers_are_offered_at_once(policy):
    """キーがあり Ollama にも到達できるなら、両方が選択肢に並ぶ。"""
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        catalog = list_local_models(s, policy, fetch_context_length=False)

    providers = {m.provider for m in catalog.selectable}
    assert providers == {"ollama", "anthropic"}
    assert any(m.local for m in catalog.selectable)
    assert any(not m.local for m in catalog.selectable)


@pytest.mark.parametrize(
    ("provider", "wanted", "expected_provider"),
    [
        ("ollama", "qwen3:14b", "ollama"),
        ("anthropic", "claude-opus-5", "anthropic"),
        ("ollama", "claude-sonnet-5", "anthropic"),
        ("anthropic", "qwen3:14b", "ollama"),
    ],
)
def test_model_choice_wins_over_the_configured_provider(
    policy, provider, wanted, expected_provider
):
    """既定のプロバイダが何であれ、選んだモデルのプロバイダで実行する。"""
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        s.provider = provider
        apply_model_selection(s, policy, model=wanted)

    assert s.provider == expected_provider
    assert s.model == wanted


def test_fallback_stays_within_the_configured_provider(policy):
    """両方使える設定でも、既定が anthropic なら Ollama へは落ちない。

    クラウドのモデルは size_bytes=0 なので、サイズだけで既定を決めると
    必ずローカルに負ける。プロバイダで先に絞ること。
    """
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        s.provider = "anthropic"
        _catalog, notes = apply_model_selection(
            s, policy, model="does-not-exist", strict=False
        )

    assert s.provider == "anthropic"
    assert s.model.startswith("claude-")
    assert any("does-not-exist" in n for n in notes)


def test_fallback_stays_on_ollama_when_that_is_the_default(policy):
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        s.provider = "ollama"
        apply_model_selection(s, policy, model="does-not-exist", strict=False)

    assert s.provider == "ollama"
    assert s.model == "qwen3:14b"


def test_fallback_crosses_providers_when_it_has_to(policy):
    """既定が ollama でも、Ollama が落ちていればクラウドへ逃がす。"""
    s = Settings()
    s.ollama_base_url = "http://127.0.0.1:1"
    s.anthropic_api_key = "sk-test"
    s.provider = "ollama"
    apply_model_selection(s, policy, model="qwen3:14b", strict=False)
    assert s.provider == "anthropic"
    assert s.model.startswith("claude-")


def test_ollama_run_does_not_route_biomni_to_anthropic(policy, monkeypatch):
    """キーが .env にあっても、Ollama を選んだランは BIOMNI_SOURCE=Ollama。

    biomni/tool/database.py は default_config を見るので、ここが Anthropic の
    ままだと DB クエリツールだけ勝手に課金される。
    """
    from biomni_hypo.config import apply_biomni_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with MockOllama(models=LOCAL) as mock:
        s = Settings()
        s.ollama_base_url = mock.base_url
        s.anthropic_api_key = "sk-test"
        s.provider = "anthropic"
        apply_model_selection(s, policy, model="qwen3:14b")
        env = apply_biomni_env(s)

    assert env["BIOMNI_SOURCE"] == "Ollama"
    assert env["LLM_SOURCE"] == "Ollama"
    assert env["BIOMNI_LLM"] == "qwen3:14b"
