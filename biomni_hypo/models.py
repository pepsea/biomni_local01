"""ローカル（Ollama）にあるモデルの探索と選択.

`/api/tags` で pull 済みモデルを列挙し、`/api/show` で最大 context 長を取り、
リソースポリシー（商用限定）でライセンスを判定する。

「何が入っていて、どれが使えて、なぜ使えないのか」を 1 つの型にまとめるのが目的。
Web アプリのモデル選択プルダウンも、ノートブックの選択セルも、CLI も、これを使う。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import requests

from biomni_hypo.config import Settings
from biomni_hypo.policy import ResourcePolicy

log = logging.getLogger(__name__)

#: システムプロンプトに加えて、会話用に最低限確保したいトークン数
MIN_CONVERSATION_TOKENS = 8192

#: num_ctx の候補（モデルの上限に収まる最大のものを選ぶ）
NUM_CTX_STEPS = (8192, 16384, 32768, 65536, 131072)

#: 埋め込み専用モデル。`ollama list` には並ぶが、チャットには使えない。
#: 選ばせると「実行したら壊れる」ので、そもそも一覧に出さない。
#: ライセンス的には通ってしまう（nomic-embed-text は Apache-2.0）ので、
#: ポリシーでは弾けない。用途で弾く必要がある。
EMBEDDING_MARKERS = ("embed", "embedding", "bge-", "gte-", "e5-", "minilm")


def is_embedding_model(name: str) -> bool:
    """埋め込み専用モデルか（チャットには使えない）。"""
    low = name.lower()
    return any(mark in low for mark in EMBEDDING_MARKERS)


@dataclass
class ModelOption:
    """選択肢になるモデル 1 件（ローカルもクラウドも同じ型で扱う）。"""

    name: str
    provider: str = "ollama"
    label: str = ""
    size_bytes: int = 0
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    max_context: int = 0
    #: ポリシー判定
    license: str = "unknown"
    allowed: bool = False
    reason: str = ""
    note: str = ""
    recommended: bool = False
    matched_by: str = ""
    #: 未取得の推奨モデル（pull を促すために一覧へ混ぜる）
    installed: bool = True
    #: データがローカルから出ないか。False なら質問文が外部へ送信される
    local: bool = True
    #: クラウドモデルの参考価格（$/1M トークン）
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / 1e9, 1)

    def suggested_num_ctx(self, requested: int) -> int:
        """このモデルで実際に使える num_ctx。

        モデルの上限を超える num_ctx を渡すと Ollama 側で切り詰められ、
        システムプロンプトが静かに欠ける。事前に丸めておく。
        """
        if self.max_context <= 0:
            return requested
        return min(requested, self.max_context)

    @property
    def display_name(self) -> str:
        return self.label or self.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "label": self.display_name,
            "local": self.local,
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "installed": self.installed,
            "size_gb": self.size_gb,
            "family": self.family,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "max_context": self.max_context,
            "license": self.license,
            "allowed": self.allowed,
            "reason": self.reason,
            "note": self.note,
            "recommended": self.recommended,
            "matched_by": self.matched_by,
        }


@dataclass
class ModelCatalog:
    """ローカルのモデル一覧と、Ollama への到達状況。"""

    models: list[ModelOption] = field(default_factory=list)
    reachable: bool = False
    base_url: str = ""
    error: str = ""

    @property
    def selectable(self) -> list[ModelOption]:
        """実際に選べるもの = pull 済み かつ ポリシー許可。"""
        return [m for m in self.models if m.installed and m.allowed]

    @property
    def blocked(self) -> list[ModelOption]:
        return [m for m in self.models if m.installed and not m.allowed]

    def get(self, name: str) -> ModelOption | None:
        for m in self.models:
            if m.name == name:
                return m
        return None

    def default(self, preferred: str = "", provider: str = "") -> ModelOption | None:
        """既定で選ぶべきモデル。

        1. preferred が選択可能ならそれ
        2. provider を指定していて、そのプロバイダに選択可能なものがあれば、その中から
        3. 推奨タグの付いた選択可能モデルのうち、いちばん大きいもの
        4. 選択可能モデルのうち、いちばん大きいもの

        Ollama と Claude を両方使える設定（scripts/set-provider.sh both）では、
        選択可能なモデルが両プロバイダに跨がる。size_bytes だけで選ぶと
        クラウドのモデル（size_bytes=0）が必ず負けて、HYPO_PROVIDER=anthropic に
        しても Ollama へ落ちてしまうので、provider で先に絞る。
        """
        if preferred:
            m = self.get(preferred)
            if m and m.installed and m.allowed:
                return m
        pool = self.selectable
        if provider:
            narrowed = [m for m in pool if m.provider == provider]
            if narrowed:
                pool = narrowed
        if not pool:
            return None
        recommended = [m for m in pool if m.recommended]
        return max(recommended or pool, key=lambda m: m.size_bytes)

    def as_table(self) -> str:
        """ノートブックと CLI 用のテキスト表。"""
        if not self.models:
            if not self.reachable:
                return f"Ollama に到達できません ({self.base_url}): {self.error}"
            return "モデルが 1 つも見つかりません。`ollama pull qwen3:14b` を実行してください。"

        rows = [f"{'':2s} {'モデル':34s} {'種別':10s} {'サイズ':>7s} {'ctx':>10s} 備考"]
        for m in self.models:
            if not m.installed:
                mark = "…"
            elif m.allowed:
                mark = "★" if m.recommended else "✓"
            else:
                mark = "✕"
            kind = "ローカル" if m.local else "クラウド"
            note = m.reason or m.note or ("未取得: ollama pull " + m.name if not m.installed else "")
            if not m.local and m.input_per_mtok:
                note = f"${m.input_per_mtok}/${m.output_per_mtok} per 1M · {note}".strip(" ·")
            size = f"{m.size_gb}GB" if m.size_bytes else "-"
            ctx = f"{m.max_context:,}" if m.max_context else "-"
            rows.append(f"{mark:2s} {m.name:34s} {kind:10s} {size:>7s} {ctx:>10s} {note[:46]}")
        rows.append("")
        if not self.reachable:
            rows.append(f"⚠️ Ollama に到達できません（{self.base_url}）。ローカルのモデルは選べません")
        rows.append("★ 推奨 / ✓ 選択可 / ✕ 不可 / … 未取得")
        if any(not m.local and m.allowed for m in self.models):
            rows.append("⚠️ クラウドのモデルを選ぶと、質問文と実行結果が外部に送信されます")
        return "\n".join(rows)


def list_local_models(
    settings: Settings,
    policy: ResourcePolicy,
    *,
    timeout: float = 5.0,
    include_not_installed: bool = True,
    fetch_context_length: bool = True,
) -> ModelCatalog:
    """Ollama から pull 済みモデルを読み込み、ポリシーで判定して返す。"""
    base = settings.ollama_base_url.rstrip("/")
    catalog = ModelCatalog(base_url=base)

    try:
        r = requests.get(f"{base}/api/tags", timeout=timeout)
        r.raise_for_status()
        entries = r.json().get("models", [])
        catalog.reachable = True
    except Exception as exc:  # noqa: BLE001 - 接続系はまとめて扱う
        catalog.error = f"{type(exc).__name__}: {exc}"
        entries = []

    for entry in entries:
        name = entry.get("name") or entry.get("model") or ""
        if not name:
            continue
        if is_embedding_model(name):
            # チャットに使えないものを選択肢に混ぜない（実行してから壊れる）
            log.debug("埋め込み専用モデルを除外: %s", name)
            continue
        details = entry.get("details") or {}
        decision = policy.check_model(name)
        model = ModelOption(
            name=name,
            size_bytes=int(entry.get("size") or 0),
            family=details.get("family", ""),
            parameter_size=details.get("parameter_size", ""),
            quantization=details.get("quantization_level", ""),
            license=decision.license,
            allowed=decision.allowed,
            reason=decision.reason,
            note=decision.note,
            recommended=decision.recommended,
            matched_by=decision.matched_by,
        )
        catalog.models.append(model)

    # context 長は /api/show への往復が要る。逐次に回すと、モデル数 × 応答時間だけ
    # 画面のモデル選択欄が空のままになる（実 Ollama はモデル読み込み中に遅い）。
    # 並列に取り、結果はキャッシュする。使えないモデルは調べても意味がないので省く。
    if fetch_context_length:
        targets = [m for m in catalog.models if m.local and m.allowed and not m.max_context]
        _fill_context_lengths(base, targets, timeout=timeout)

    catalog.models += _cloud_models(settings, policy)
    catalog.models.sort(
        key=lambda m: (not m.recommended, not m.allowed, not m.installed, -m.size_bytes, m.name)
    )

    if include_not_installed:
        installed = {m.name for m in catalog.models}
        for name in policy.allowed_model_names():
            if name in installed:
                continue
            d = policy.check_model(name)
            catalog.models.append(
                ModelOption(
                    name=name,
                    license=d.license,
                    allowed=d.allowed,
                    note=d.note,
                    recommended=d.recommended,
                    matched_by=d.matched_by,
                    installed=False,
                )
            )
    return catalog


def _cloud_models(settings: Settings, policy: ResourcePolicy) -> list[ModelOption]:
    """クラウド（Claude API）のモデルを一覧に混ぜる。

    API キーが無ければ `installed=False` で出す。何を設定すれば使えるかが
    分かるようにするため、黙って隠さない。
    """
    out: list[ModelOption] = []
    for provider_name, provider in policy.providers().items():
        if provider.get("local", True):
            continue
        has_key = bool(settings.anthropic_api_key) if provider_name == "anthropic" else False
        env_var = provider.get("requires_env", "")
        for entry in provider.get("models") or []:
            out.append(
                ModelOption(
                    name=entry["name"],
                    provider=provider_name,
                    label=entry.get("label", ""),
                    max_context=int(entry.get("context") or 0),
                    license=provider.get("label", provider_name),
                    allowed=True,
                    installed=has_key,
                    local=False,
                    recommended=bool(entry.get("recommended")) and has_key,
                    input_per_mtok=float(entry.get("input_per_mtok") or 0),
                    output_per_mtok=float(entry.get("output_per_mtok") or 0),
                    note=entry.get("note", ""),
                    reason="" if has_key else f"{env_var} が未設定です",
                    matched_by="provider",
                )
            )
    return out


#: (base_url, model) -> context 長。プロセスの生存中は変わらないのでキャッシュする
_CONTEXT_CACHE: dict[tuple[str, str], int] = {}


def _fill_context_lengths(
    base_url: str, models: list[ModelOption], *, timeout: float = 5.0
) -> None:
    """複数モデルの context 長をまとめて取る（並列 + キャッシュ）。"""
    todo = []
    for model in models:
        cached = _CONTEXT_CACHE.get((base_url, model.name))
        if cached is not None:
            model.max_context = cached
        else:
            todo.append(model)
    if not todo:
        return
    # Ollama を叩きすぎない程度に絞る。8 並列なら 20 モデルでも 3 往復ぶん
    with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
        futures = {
            pool.submit(_max_context, base_url, m.name, timeout=timeout): m for m in todo
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                value = future.result()
            except Exception as exc:  # noqa: BLE001 - 取れなくても致命ではない
                log.debug("context 長の取得に失敗 (%s): %s", model.name, exc)
                value = 0
            model.max_context = value
            _CONTEXT_CACHE[(base_url, model.name)] = value


def _max_context(base_url: str, name: str, timeout: float = 5.0) -> int:
    """`/api/show` の model_info から `<arch>.context_length` を取る。"""
    try:
        r = requests.post(f"{base_url}/api/show", json={"model": name}, timeout=timeout)
        r.raise_for_status()
        info = r.json().get("model_info") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("context 長を取得できませんでした (%s): %s", name, exc)
        return 0
    for key, value in info.items():
        if key.endswith(".context_length"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def resolve_num_ctx(model: ModelOption | None, requested: int, prompt_tokens: int = 0) -> tuple[int, str]:
    """使うべき num_ctx と、その理由を返す。

    - モデルの上限を超えていたら丸める（超えると Ollama が黙って切り詰める）
    - システムプロンプトを引いた残りが会話に足りなければ警告する
    """
    if model is None or not model.local:
        # クラウドのモデルは num_ctx を指定しない
        return requested, ""

    resolved = model.suggested_num_ctx(requested)
    notes = []
    if resolved < requested:
        notes.append(f"{model.name} の上限 {model.max_context:,} に合わせて {requested:,} から丸めました")

    if prompt_tokens:
        remaining = resolved - prompt_tokens
        if remaining < MIN_CONVERSATION_TOKENS:
            notes.append(
                f"システムプロンプト {prompt_tokens:,} トークンを引くと残り {remaining:,} しかありません。"
                f"ツールモジュールを減らすか、context の大きいモデルを選んでください"
            )
    return resolved, " / ".join(notes)


class ModelNotAvailable(ValueError):
    """選んだモデルが使えない（未取得 / ライセンス不可 / Ollama 未起動）。"""


def apply_model_selection(
    settings: Settings,
    policy: ResourcePolicy,
    *,
    model: str | None = None,
    catalog: ModelCatalog | None = None,
    strict: bool = True,
) -> tuple[ModelCatalog, list[str]]:
    """モデルを選び、settings をその場で書き換える。

    API・ノートブック・CLI がすべてこの関数を通るようにして、
    「選べるモデルの基準」が 1 箇所に集まるようにする。

    やること:
      1. モデル名を決める（引数 > settings.model > カタログの既定）
      2. ライセンスと取得状況を確認する
      3. num_ctx をモデルの上限に丸める

    Args:
        strict: True なら使えないモデルで ModelNotAvailable を投げる。
            False なら警告メッセージを返すだけで settings は変えない。

    Returns:
        (カタログ, 人間向けの注意メッセージ)
    """
    catalog = catalog or list_local_models(settings, policy)
    notes: list[str] = []
    wanted = model or settings.model

    if not catalog.reachable and not catalog.selectable:
        # Ollama も落ちていて、クラウドも使えない
        message = f"Ollama に到達できません ({catalog.base_url}): {catalog.error}"
        if strict:
            raise ModelNotAvailable(message)
        return catalog, [message]
    if not catalog.reachable:
        notes.append(f"Ollama に到達できません（{catalog.base_url}）。クラウドのモデルのみ選べます")

    selected = catalog.get(wanted)

    if selected is None:
        fallback = catalog.default(provider=settings.provider)
        message = f"{wanted} はローカルにありません（ollama pull {wanted}）"
        if strict:
            hint = f" 使えるモデル: {', '.join(m.name for m in catalog.selectable) or 'なし'}"
            raise ModelNotAvailable(message + "。" + hint)
        notes.append(message)
        selected = fallback
    elif not selected.installed:
        message = f"{wanted} は未取得です（ollama pull {wanted}）"
        if strict:
            raise ModelNotAvailable(message)
        notes.append(message)
        selected = catalog.default(provider=settings.provider)
    elif not selected.allowed:
        message = f"{wanted} は商用利用ポリシーにより使用できません: {selected.reason}"
        if strict:
            raise ModelNotAvailable(message)
        notes.append(message)
        selected = catalog.default(provider=settings.provider)

    if selected is None:
        message = "使用できるモデルが 1 つもありません（ollama pull qwen3:14b）"
        if strict:
            raise ModelNotAvailable(message)
        return catalog, [*notes, message]

    if selected.name != wanted:
        notes.append(f"{selected.name} を代わりに選びました")

    settings.provider = selected.provider
    settings.model = selected.name
    if not selected.local:
        notes.append(
            "クラウドのモデルです。質問文と実行結果が外部に送信されます"
            "（オフラインモードとは併用できません）"
        )
        if settings.offline_mode:
            raise ModelNotAvailable(
                "オフラインモードではクラウドのモデルを使えません。"
                "ローカルのモデルを選ぶか、オフラインモードを解除してください。"
            )
    resolved, note = resolve_num_ctx(selected, settings.num_ctx)
    if note:
        notes.append(note)
    settings.num_ctx = resolved
    return catalog, notes


#: 後方互換のための別名
LocalModel = ModelOption
list_models = list_local_models
