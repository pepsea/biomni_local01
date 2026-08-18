"""ローカル（Ollama）にあるモデルの探索と選択.

`/api/tags` で pull 済みモデルを列挙し、`/api/show` で最大 context 長を取り、
リソースポリシー（商用限定）でライセンスを判定する。

「何が入っていて、どれが使えて、なぜ使えないのか」を 1 つの型にまとめるのが目的。
Web アプリのモデル選択プルダウンも、ノートブックの選択セルも、CLI も、これを使う。
"""

from __future__ import annotations

import logging
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


@dataclass
class LocalModel:
    """ローカルに pull 済みのモデル 1 件。"""

    name: str
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
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

    models: list[LocalModel] = field(default_factory=list)
    reachable: bool = False
    base_url: str = ""
    error: str = ""

    @property
    def selectable(self) -> list[LocalModel]:
        """実際に選べるもの = pull 済み かつ ポリシー許可。"""
        return [m for m in self.models if m.installed and m.allowed]

    @property
    def blocked(self) -> list[LocalModel]:
        return [m for m in self.models if m.installed and not m.allowed]

    def get(self, name: str) -> LocalModel | None:
        for m in self.models:
            if m.name == name:
                return m
        return None

    def default(self, preferred: str = "") -> LocalModel | None:
        """既定で選ぶべきモデル。

        1. preferred が選択可能ならそれ
        2. 推奨タグの付いた選択可能モデルのうち、いちばん大きいもの
        3. 選択可能モデルのうち、いちばん大きいもの
        """
        if preferred:
            m = self.get(preferred)
            if m and m.installed and m.allowed:
                return m
        pool = self.selectable
        if not pool:
            return None
        recommended = [m for m in pool if m.recommended]
        return max(recommended or pool, key=lambda m: m.size_bytes)

    def as_table(self) -> str:
        """ノートブックと CLI 用のテキスト表。"""
        if not self.reachable:
            return f"Ollama に到達できません ({self.base_url}): {self.error}"
        if not self.models:
            return "モデルが 1 つも見つかりません。`ollama pull qwen3:14b` を実行してください。"

        rows = [f"{'':2s} {'モデル':34s} {'サイズ':>7s} {'ctx':>8s} {'ライセンス':22s} 備考"]
        for m in self.models:
            if not m.installed:
                mark = "…"
            elif m.allowed:
                mark = "★" if m.recommended else "✓"
            else:
                mark = "✕"
            note = m.reason or m.note or ("未取得: ollama pull " + m.name if not m.installed else "")
            size = f"{m.size_gb}GB" if m.size_bytes else "-"
            ctx = f"{m.max_context:,}" if m.max_context else "-"
            rows.append(f"{mark:2s} {m.name:34s} {size:>7s} {ctx:>8s} {m.license:22s} {note[:44]}")
        rows.append("")
        rows.append("★ 推奨 / ✓ 選択可 / ✕ ライセンス不可 / … 未取得")
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
        details = entry.get("details") or {}
        decision = policy.check_model(name)
        model = LocalModel(
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
        # 使えないモデルの context 長を調べても意味がないので許可済みだけ問い合わせる
        if fetch_context_length and decision.allowed:
            model.max_context = _max_context(base, name, timeout=timeout)
        catalog.models.append(model)

    catalog.models.sort(key=lambda m: (not m.recommended, not m.allowed, -m.size_bytes, m.name))

    if include_not_installed:
        installed = {m.name for m in catalog.models}
        for name in policy.allowed_model_names():
            if name in installed:
                continue
            d = policy.check_model(name)
            catalog.models.append(
                LocalModel(
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


def resolve_num_ctx(model: LocalModel | None, requested: int, prompt_tokens: int = 0) -> tuple[int, str]:
    """使うべき num_ctx と、その理由を返す。

    - モデルの上限を超えていたら丸める（超えると Ollama が黙って切り詰める）
    - システムプロンプトを引いた残りが会話に足りなければ警告する
    """
    if model is None:
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

    if not catalog.reachable:
        message = f"Ollama に到達できません ({catalog.base_url}): {catalog.error}"
        if strict:
            raise ModelNotAvailable(message)
        return catalog, [message]

    selected = catalog.get(wanted)

    if selected is None:
        fallback = catalog.default()
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
        selected = catalog.default()
    elif not selected.allowed:
        message = f"{wanted} は商用利用ポリシーにより使用できません: {selected.reason}"
        if strict:
            raise ModelNotAvailable(message)
        notes.append(message)
        selected = catalog.default()

    if selected is None:
        message = "使用できるモデルが 1 つもありません（ollama pull qwen3:14b）"
        if strict:
            raise ModelNotAvailable(message)
        return catalog, [*notes, message]

    if selected.name != wanted:
        notes.append(f"{selected.name} を代わりに選びました")

    settings.model = selected.name
    resolved, note = resolve_num_ctx(selected, settings.num_ctx)
    if note:
        notes.append(note)
    settings.num_ctx = resolved
    return catalog, notes
