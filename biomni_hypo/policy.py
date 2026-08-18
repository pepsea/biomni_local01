"""商用利用限定のリソースポリシー（docs/design/05-commercial-licensing.md §5.2）.

Biomni の commercial_mode=True はデータセット・ライブラリ・know-how しか絞らず、
ツールは絞らない。このモジュールがその穴を埋め、既定拒否を敷く。

強制ポイントは 4 つ:
  1. データ取得時   -> allowed_dataset_names() を A1(expected_data_lake_files=...) に渡す
  2. ツール登録時   -> filter_agent_tools() で tool_registry / module2api から除去
  3. コード実行直前 -> inspect_code() で静的検査（最後の砦）
  4. モデル選択時   -> check_model()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from biomni_hypo.schemas import Resource, ResourceKind

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "resource_policy.yaml"


@dataclass(frozen=True)
class Decision:
    """許可判定の結果。拒否理由は必ず人間が読める形で持つ。"""

    allowed: bool
    review_required: bool = False
    reason: str = ""
    license: str = "unknown"
    attribution: str = ""
    url: str = ""
    note: str = ""
    #: モデル判定でのみ使う。推奨モデルかどうか
    recommended: bool = False
    #: どの規則で判定したか（"exact" / "family" / "deny_family" / "default"）
    matched_by: str = ""


ALLOWED = Decision(allowed=True)


@dataclass
class Violation:
    kind: str  # "tool" | "dataset"
    name: str
    reason: str
    line: int = -1

    def as_message(self) -> str:
        where = f" (行 {self.line})" if self.line > 0 else ""
        return f"[{self.kind}] {self.name}{where}: {self.reason}"


@dataclass
class ResourcePolicy:
    version: int = 0
    mode: str = "commercial_only"
    _datasets: dict[str, dict[str, Any]] = field(default_factory=dict)
    _dataset_review: set[str] = field(default_factory=set)
    _tool_deny: dict[str, str] = field(default_factory=dict)
    _tool_review: dict[str, str] = field(default_factory=dict)
    _library_deny: set[str] = field(default_factory=set)
    _models: dict[str, dict[str, Any]] = field(default_factory=dict)
    _model_allow_families: list[dict[str, Any]] = field(default_factory=list)
    _model_deny_families: list[dict[str, Any]] = field(default_factory=list)
    _dataset_deny_reason: str = "許可リストに無いデータセット"
    _model_deny_reason: str = "許可リストに無いモデル"

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str | Path | None = None) -> ResourcePolicy:
        path = Path(path) if path else DEFAULT_POLICY_PATH
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResourcePolicy:
        pol = cls(version=int(raw.get("policy_version", 0)), mode=raw.get("mode", "commercial_only"))

        ds = raw.get("datasets") or {}
        for entry in ds.get("allow") or []:
            pol._datasets[entry["name"]] = entry
        for entry in ds.get("review_required") or []:
            pol._datasets[entry["name"]] = entry
            pol._dataset_review.add(entry["name"])
        pol._dataset_deny_reason = ds.get("deny_reason_default", pol._dataset_deny_reason)

        tools = raw.get("tools") or {}
        for entry in tools.get("deny") or []:
            pol._tool_deny[entry["name"]] = entry.get("reason", "ポリシーにより不可")
        for entry in tools.get("review_required") or []:
            pol._tool_review[entry["name"]] = entry.get("reason", "要レビュー")

        libs = raw.get("libraries") or {}
        pol._library_deny = set(libs.get("deny") or [])

        models = raw.get("models") or {}
        for entry in models.get("allow") or []:
            pol._models[entry["name"]] = entry
        pol._model_allow_families = list(models.get("allow_families") or [])
        pol._model_deny_families = list(models.get("deny_families") or [])
        pol._model_deny_reason = models.get("deny_reason_default", pol._model_deny_reason)
        return pol

    # -------------------------------------------------------------- accessors

    def allowed_dataset_names(self) -> list[str]:
        """A1(expected_data_lake_files=...) に渡すリスト。

        これを渡さないと A1.__init__ がデータレイクを一括ダウンロードする
        （docs/design/04 §4.4）。
        """
        return sorted(self._datasets)

    def allowed_model_names(self) -> list[str]:
        """明示的に列挙している推奨モデル名（未取得でも UI に出すため）。

        ファミリー規則で許可されるモデルはここには出ない。
        実際に使えるモデルの一覧は biomni_hypo.models.list_local_models() を使う。
        """
        return sorted(self._models)

    def denied_tool_names(self) -> list[str]:
        return sorted(self._tool_deny)

    # --------------------------------------------------------------- checkers

    def check_dataset(self, name: str) -> Decision:
        entry = self._datasets.get(name)
        if entry is None:
            return Decision(allowed=False, reason=self._dataset_deny_reason)
        return Decision(
            allowed=True,
            review_required=name in self._dataset_review,
            license=entry.get("license", "unknown"),
            attribution=entry.get("attribution", ""),
            url=entry.get("url", ""),
            note=entry.get("note", ""),
        )

    def check_tool(self, name: str) -> Decision:
        if name in self._tool_deny:
            return Decision(allowed=False, reason=self._tool_deny[name])
        if name in self._tool_review:
            return Decision(allowed=True, review_required=True, note=self._tool_review[name])
        return ALLOWED

    def check_library(self, name: str) -> Decision:
        if name in self._library_deny:
            return Decision(allowed=False, reason="商用利用ポリシーにより不可")
        return ALLOWED

    def check_model(self, name: str) -> Decision:
        """ローカルにある任意のモデル名を判定する。

        Ollama のモデル名はタグ付き（`qwen3:8b-instruct-q4_K_M`）なので、
        完全一致だけでは実際に pull されているモデルを拾えない。
        ファミリー名の前方一致で判定し、拒否ファミリーを許可より優先する。
        """
        family = model_family(name)

        for entry in self._model_deny_families:
            if family.startswith(str(entry["match"]).lower()):
                return Decision(
                    allowed=False,
                    reason=entry.get("reason", "商用利用ポリシーにより不可"),
                    license=entry.get("license", "unknown"),
                    matched_by="deny_family",
                )

        exact = self._models.get(name)
        if exact is not None:
            return Decision(
                allowed=True,
                license=exact.get("license", "unknown"),
                note=exact.get("note", ""),
                recommended=bool(exact.get("recommended")),
                matched_by="exact",
            )

        # 完全一致しなくても、推奨リストと同じファミリーなら推奨扱いにする
        recommended_families = {
            model_family(n) for n, e in self._models.items() if e.get("recommended")
        }
        for entry in self._model_allow_families:
            if family.startswith(str(entry["match"]).lower()):
                return Decision(
                    allowed=True,
                    license=entry.get("license", "unknown"),
                    note=entry.get("note", ""),
                    recommended=family in recommended_families,
                    matched_by="family",
                )

        return Decision(allowed=False, reason=self._model_deny_reason, matched_by="default")

    def recommended_model_names(self) -> list[str]:
        return sorted(n for n, e in self._models.items() if e.get("recommended"))

    # ------------------------------------------------------ code static check

    def inspect_code(self, code: str) -> list[Violation]:
        """<execute> のコードを実行前に検査する（強制ポイント 3 / 最後の砦）.

        1・2 をすり抜けた経路（LLM が直接 import を書く、URL を直に叩く等）を
        ここで止める。検出は行単位で、コメント行は無視する。
        """
        violations: list[Violation] = []
        for lineno, line in enumerate(code.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for tool, reason in self._tool_deny.items():
                if re.search(rf"\b{re.escape(tool)}\b", line):
                    violations.append(Violation("tool", tool, reason, lineno))
            for m in re.finditer(r"""['"]([\w./-]+\.(?:csv|tsv|parquet|pkl|json|obo|txt))['"]""", line):
                name = Path(m.group(1)).name
                # データレイクに存在しうる名前だけを対象にする。
                # ユーザーがアップロードした作業ファイルは対象外（別途 uploads で管理）。
                if name in self._datasets:
                    continue
                if self._looks_like_data_lake_file(name):
                    violations.append(
                        Violation("dataset", name, self._dataset_deny_reason, lineno)
                    )
        return violations

    #: 非商用として除外された既知のデータセット（誤って参照されたら止める）
    KNOWN_NON_COMMERCIAL = (
        "msigdb_",
        "omim",
        "disgenet",
        "bindingdb",
        "ddinter_",
        "mirtarbase",
        "mirdb",
        "mcpas",
        "enamine",
        "evebio_",
        "cosmic_",
    )

    def _looks_like_data_lake_file(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered.startswith(p) or p in lowered for p in self.KNOWN_NON_COMMERCIAL)

    # ------------------------------------------------------------- resources

    def describe_dataset(self, name: str, step_idxs: list[int] | None = None) -> Resource:
        d = self.check_dataset(name)
        return Resource(
            kind=ResourceKind.DATASET,
            name=name,
            license=d.license,
            attribution=d.attribution,
            url=d.url,
            commercial_ok=d.allowed,
            review_required=d.review_required,
            step_idxs=step_idxs or [],
        )


def model_family(name: str) -> str:
    """Ollama のモデル名からファミリー名を取り出す。

    >>> model_family("qwen3:8b-instruct-q4_K_M")
    'qwen3'
    >>> model_family("library/gpt-oss:20b")
    'gpt-oss'
    >>> model_family("hf.co/user/Qwen3-14B-GGUF:Q4_K_M")
    'qwen3-14b-gguf'
    """
    base = name.split(":", 1)[0]
    base = base.rsplit("/", 1)[-1]
    return base.strip().lower()
