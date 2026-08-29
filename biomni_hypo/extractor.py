"""トレース -> 構造化された仮説（docs/design/03-evidence-model.md §3.3）.

方針: A1 の <solution> をそのまま信用しない。
トレースから機械抽出した根拠候補を渡し、**その eid からしか選べない**形で
仮説を書かせる。未知の eid が出てきたらその根拠は捨てる。
これでローカル LLM が PMID を捏造しても、仮説の根拠には入り込めない。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from biomni_hypo.config import Settings
from biomni_hypo.schemas import (
    Evidence,
    EvidenceCandidate,
    Hypothesis,
    ReasoningPoint,
    ResourceKind,
    Stance,
    Step,
    StepKind,
    TestPlan,
)

log = logging.getLogger(__name__)

HYPOTHESIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "answer_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"eid": {"type": "string"}, "why": {"type": "string"}},
                "required": ["eid"],
            },
        },
        "reasoning": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "finding": {"type": "string"},
                    "stance": {"type": "string", "enum": ["supports", "refutes", "context"]},
                    "weight": {"type": "string", "enum": ["decisive", "supporting", "weak"]},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"eid": {"type": "string"}, "why": {"type": "string"}},
                            "required": ["eid"],
                        },
                    },
                },
                "required": ["point", "finding"],
            },
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "novelty": {
                        "type": "string",
                        "enum": ["established", "emerging", "speculative"],
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "eid": {"type": "string"},
                                "stance": {
                                    "type": "string",
                                    "enum": ["supports", "refutes", "context"],
                                },
                                "claim_span": {"type": "string"},
                                "why": {"type": "string"},
                            },
                            "required": ["eid", "stance", "why"],
                        },
                    },
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "test_plan": {
                        "type": "object",
                        "properties": {
                            "experiment": {"type": "string"},
                            "readout": {"type": "string"},
                            "controls": {"type": "array", "items": {"type": "string"}},
                            "feasibility": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "estimated_effort": {"type": "string"},
                        },
                        "required": ["experiment", "readout"],
                    },
                },
                "required": ["statement", "rationale", "confidence", "evidence", "test_plan"],
            },
        },
    },
    "required": ["answer", "reasoning", "hypotheses"],
}

PROMPT_TEMPLATE = """あなたは生物医学の研究者です。以下の調査ログから、(1) 質問への回答 と (2) 検証可能な仮説（最大 {max_hypotheses} 件）を抽出してください。

# 研究課題
{question}

# エージェントの結論（参考。ここに書かれた識別子は根拠として使えません）
{solution_text}

# 調査で実行した手順
{steps_block}

# 使用できる根拠（ID | 種別 | 識別子 | ステップ | 抜粋）
{candidates_block}

# 厳守事項
0. answer は日本語で 3〜5 文。質問に正面から答えること。
   調査で分かったことだけを書き、分からなかったことは「分からなかった」と書くこと。
   answer_evidence には、その回答を支える根拠 ID を挙げること。
0-a. reasoning に、その回答に至った論点を 2〜6 件挙げること。**結論だけでは不十分です。**
   1 件ごとに:
     point   : 検討した論点。「〜か」という問いの形で書く
               例:「FGFR2 の乳がんとの関連は公共データで再現するか」
     finding : それを調べて実際に分かったこと。数値・遺伝子名・件数を含めること
     stance  : その論点が結論を supports（支持）/ refutes（反証）/ context（判断材料）のどれか
     weight  : decisive（これが無ければ結論が変わる）/ supporting（補強）/ weak（弱い）
     evidence: その論点を裏付ける根拠 ID
   結論と食い違う所見や、検討したが採らなかった解釈も、stance を "refutes" にして
   必ず含めること。都合のよい論点だけを並べてはいけません。
0-b. uncertainties に、調べたが分からなかったこと・この回答の限界を挙げること。
   無ければ空配列。「無い」と嘘を書くよりは空にすること。
1. evidence / answer_evidence / reasoning[].evidence の eid は、
   上の「使用できる根拠」に載っている ID のみを使うこと。
   リストに無い ID・自分で考えた PMID や遺伝子 ID を書いてはいけません。
2. statement に PMID や DOI やアクセッション番号を直接書かないこと。識別子は evidence でのみ表現します。
3. 根拠が見つからない着想も、evidence を空配列にして出力してよい（捨てないこと）。
4. statement は「何が・何に・どう影響するか」を含む 1 文にすること。
5. 反証する根拠がある場合は stance を "refutes" にして必ず含めること。
6. JSON のみを出力すること。前後に説明文を付けないこと。

# 出力する JSON の形
{{"answer": "調査の結果、…（3〜5 文）",
  "answer_evidence": [{{"eid": "E1", "why": "…"}}],
  "reasoning": [{{"point": "…か", "finding": "…", "stance": "supports|refutes|context",
    "weight": "decisive|supporting|weak", "evidence": [{{"eid": "E1", "why": "…"}}]}}],
  "uncertainties": ["…"],
  "hypotheses": [{{"statement": "...", "rationale": "...", "confidence": "high|medium|low",
  "novelty": "established|emerging|speculative",
  "evidence": [{{"eid": "E1", "stance": "supports", "claim_span": "...", "why": "..."}}],
  "assumptions": ["..."],
  "test_plan": {{"experiment": "...", "readout": "...", "controls": ["..."],
    "feasibility": "high|medium|low", "estimated_effort": "..."}}}}]}}
"""


@dataclass
class ExtractionResult:
    answer: str = ""
    answer_evidence: list[Evidence] = field(default_factory=list)
    answer_reasoning: list[ReasoningPoint] = field(default_factory=list)
    answer_uncertainties: list[str] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    candidates: list[EvidenceCandidate] = field(default_factory=list)
    unknown_eids: list[str] = field(default_factory=list)
    raw_response: str = ""
    parse_error: str = ""
    #: モデルが reasoning として返した項目数と、形が合わず使えなかった数。
    #: 「モデルが返さなかった」と「こちらが捨てた」を区別するために持つ
    reasoning_seen: int = 0
    reasoning_dropped: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.answer or self.hypotheses) and not self.parse_error


# --------------------------------------------------------------- 根拠候補の構築


def build_candidates(steps: Iterable[Step]) -> list[EvidenceCandidate]:
    """トレースから根拠候補を作る。ここに無いものは仮説の根拠になれない。

    候補になるのは:
      - observation から抽出された識別子（文献・DB レコード）
      - execute が実際に読んだデータセット / ユーザーファイル
      - エラーなく完了した計算そのもの
    """
    candidates: list[EvidenceCandidate] = []
    seen: set[tuple[str, str]] = set()
    counter = 0

    def add(kind: ResourceKind, identifier: str, excerpt: str, step_idx: int, url: str = "") -> None:
        nonlocal counter
        key = (kind.value, identifier.upper())
        if key in seen:
            return
        seen.add(key)
        counter += 1
        candidates.append(
            EvidenceCandidate(
                eid=f"E{counter}",
                kind=kind,
                identifier=identifier,
                excerpt=excerpt,
                step_idx=step_idx,
                url=url,
            )
        )

    steps = list(steps)
    for step in steps:
        for c in step.citations:
            add(c.kind, c.identifier, c.excerpt, c.step_idx if c.step_idx >= 0 else step.idx, c.url)

    for step in steps:
        if step.kind != StepKind.EXECUTE:
            continue
        for ds in step.datasets:
            add(ResourceKind.DATASET, ds, _code_excerpt(step.code, ds), step.idx)
        for uf in step.user_files:
            add(ResourceKind.USER_FILE, uf, _code_excerpt(step.code, uf), step.idx)
        if step.code and not step.error:
            add(
                ResourceKind.COMPUTATION,
                f"step{step.idx}",
                _first_lines(step.code, 3),
                step.idx,
            )
    return candidates


def _code_excerpt(code: str, needle: str, window: int = 100) -> str:
    pos = code.find(needle)
    if pos < 0:
        return _first_lines(code, 2)
    lo, hi = max(0, pos - window), min(len(code), pos + len(needle) + window)
    return re.sub(r"\s+", " ", code[lo:hi]).strip()


def _first_lines(text: str, n: int) -> str:
    return " / ".join(line.strip() for line in text.splitlines()[:n] if line.strip())


def format_steps_block(steps: Iterable[Step], max_chars: int = 6000) -> str:
    lines: list[str] = []
    for s in steps:
        if s.kind == StepKind.EXECUTE:
            tools = ", ".join(t.name for t in s.tools) or "-"
            data = ", ".join(s.datasets + s.user_files) or "-"
            lines.append(f"[{s.idx}] 実行  ツール: {tools} / データ: {data}")
        elif s.kind == StepKind.OBSERVATION:
            lines.append(f"[{s.idx}] 観測  {_squeeze(s.text, 300)}")
        elif s.kind == StepKind.POLICY_BLOCKED:
            lines.append(f"[{s.idx}] ブロック  {_squeeze(s.text, 150)}")
        elif s.kind == StepKind.THINK:
            lines.append(f"[{s.idx}] 思考  {_squeeze(s.text, 200)}")
    block = "\n".join(lines)
    return block[:max_chars]


def format_candidates_block(candidates: Iterable[EvidenceCandidate], max_items: int = 80) -> str:
    rows = [c.as_prompt_line() for c in list(candidates)[:max_items]]
    return "\n".join(rows) if rows else "(根拠候補なし)"


def _squeeze(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n] + ("…" if len(t) > n else "")


# ------------------------------------------------------------------- パース


def parse_response(raw: str, candidates: Iterable[EvidenceCandidate]) -> ExtractionResult:
    """LLM 応答を Hypothesis に変換する。未知 eid はここで落とす（純関数）。"""
    by_eid = {c.eid: c for c in candidates}
    result = ExtractionResult(candidates=list(by_eid.values()), raw_response=raw)

    payload = _loads_lenient(raw)
    if payload is None:
        result.parse_error = "JSON としてパースできませんでした"
        return result

    result.answer = str(payload.get("answer", "")).strip()
    result.answer_evidence = _build_evidence(payload.get("answer_evidence"), by_eid, result)
    result.answer_reasoning = _build_reasoning(payload.get("reasoning"), by_eid, result)
    result.answer_uncertainties = [
        str(u).strip() for u in (payload.get("uncertainties") or []) if str(u).strip()
    ]

    items = payload.get("hypotheses")
    if not isinstance(items, list):
        result.parse_error = "'hypotheses' 配列がありません"
        return result

    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not str(item.get("statement", "")).strip():
            continue
        evidences = _build_evidence(item.get("evidence"), by_eid, result)
        tp = item.get("test_plan") or {}
        result.hypotheses.append(
            Hypothesis(
                id=f"h_{i}",
                statement=str(item["statement"]).strip(),
                rationale=str(item.get("rationale", "")).strip(),
                confidence=_enum(item.get("confidence"), ("high", "medium", "low"), "medium"),
                novelty=_enum(
                    item.get("novelty"), ("established", "emerging", "speculative"), "emerging"
                ),
                evidence=evidences,
                assumptions=[str(a) for a in (item.get("assumptions") or []) if str(a).strip()],
                test_plan=TestPlan(
                    experiment=str(tp.get("experiment", "")),
                    readout=str(tp.get("readout", "")),
                    controls=[str(c) for c in (tp.get("controls") or [])],
                    feasibility=_enum(tp.get("feasibility"), ("high", "medium", "low"), "medium"),
                    estimated_effort=str(tp.get("estimated_effort", "")),
                ),
            )
        )

    if not result.hypotheses and not result.answer:
        result.parse_error = result.parse_error or "有効な回答も仮説も得られませんでした"
    return result


def _build_evidence(
    raw: Any, by_eid: dict[str, EvidenceCandidate], result: ExtractionResult
) -> list[Evidence]:
    """LLM が挙げた根拠 ID を Evidence に変換する。未知の ID はここで落とす。"""
    out: list[Evidence] = []
    for ev in raw or []:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("eid", "")).strip()
        cand = by_eid.get(eid)
        if cand is None:
            # トレースに存在しない ID = 幻覚。主張自体は残し、根拠だけ落とす。
            result.unknown_eids.append(eid)
            continue
        out.append(
            Evidence(
                eid=cand.eid,
                kind=cand.kind,
                identifier=cand.identifier,
                stance=_stance(ev.get("stance")),
                claim_span=str(ev.get("claim_span", ""))[:400],
                why=str(ev.get("why", ""))[:600],
                excerpt=cand.excerpt,  # 抜粋は必ず実テキストから。LLM には書かせない
                step_idx=cand.step_idx,
                url=cand.url,
            )
        )
    return out


#: point / finding に使われがちなキー名。
#: モデルは仕様どおりの名前を使うとは限らない。実測で、どのモデルも
#: それぞれ違う崩し方をした。形が違うだけの論点を捨てると、画面には
#: 「論点を抽出できませんでした」としか出ず、原因が分からない。
_POINT_KEYS = ("point", "question", "claim", "argument", "issue", "topic", "title", "statement")
_FINDING_KEYS = (
    "finding", "observation", "detail", "evidence_summary",
    "result", "note", "rationale", "summary", "explanation",
)


def _reasoning_items(raw: Any) -> list[Any]:
    """モデルが返した reasoning を、扱える並びにする。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, dict):
        for key in ("reasoning", "points", "items", "list"):
            inner = raw.get(key)
            if isinstance(inner, list):
                return list(inner)
        return [raw]                    # 論点 1 件を配列に入れ忘れた形
    if isinstance(raw, list):
        return list(raw)
    return []


def _first_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _build_reasoning(
    raw: Any, by_eid: dict[str, EvidenceCandidate], result: ExtractionResult
) -> list[ReasoningPoint]:
    """論点を組み立てる。

    根拠は _build_evidence を通すので、幻覚 ID は論点ごと捨てるのではなく
    根拠だけが落ちる。論点そのものは残す（「根拠が無い論点」も情報である）。

    形の揺れは拾う。キー名が違う・文字列だけ・配列に入っていない、は
    すべて実際に起きた（docs/design/30）。拾えなかった数は数えておくこと。
    """
    items = _reasoning_items(raw)
    result.reasoning_seen = len(items)
    out: list[ReasoningPoint] = []
    for item in items:
        if isinstance(item, str):
            # 論点を 1 行の文字列で返すモデルがある。問いだけでも情報になる
            out.append(ReasoningPoint(point=item.strip()[:400], finding=""))
            continue
        if not isinstance(item, dict):
            continue
        point = _first_text(item, _POINT_KEYS)
        finding = _first_text(item, _FINDING_KEYS)
        if not point and not finding:
            continue
        out.append(
            ReasoningPoint(
                # 論点が無く所見だけなら、所見を論点として立てる（捨てるよりまし）
                point=(point or finding)[:400],
                finding=(finding if point else "")[:800],
                stance=_stance(item.get("stance")),
                weight=_weight(item.get("weight")),
                evidence=_build_evidence(item.get("evidence"), by_eid, result),
            )
        )
    result.reasoning_dropped = len(items) - len(out)
    return out


def _weight(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in ("decisive", "supporting", "weak") else "supporting"


def _stance(value: Any) -> Stance:
    try:
        return Stance(str(value))
    except ValueError:
        return Stance.SUPPORTS


def _enum(value: Any, allowed: tuple[str, ...], default: str) -> Any:
    v = str(value).lower()
    return v if v in allowed else default


def _loads_lenient(raw: str) -> dict[str, Any] | None:
    """素の JSON、```json フェンス、前後に散文が付いた出力を許容する。"""
    if not raw:
        return None
    for text in _json_candidates(raw):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _json_candidates(raw: str) -> list[str]:
    out = [raw.strip()]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        out.append(fence.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        out.append(raw[start : end + 1])
    return out


# ------------------------------------------------------------------ 実行本体


class HypothesisExtractor:
    """LLM を呼んで仮説を抽出する。LLM は差し替え可能（テスト用）。"""

    def __init__(self, settings: Settings, llm: Any = None) -> None:
        self.settings = settings
        self._llm = llm

    @property
    def llm(self) -> Any:
        if self._llm is None:
            from biomni_hypo.llm import build_llm

            # Claude は温度を受け付けないモデルがあるので、Ollama のときだけ渡す
            temperature = (
                self.settings.extractor_temperature
                if self.settings.provider == "ollama"
                else None
            )
            self._llm = build_llm(
                self.settings,
                model=self.settings.extractor_model_name(),
                temperature=temperature,
                fmt=HYPOTHESIS_JSON_SCHEMA if self.settings.provider == "ollama" else None,
            )
        return self._llm

    def build_prompt(
        self,
        question: str,
        solution_text: str,
        steps: Iterable[Step],
        candidates: Iterable[EvidenceCandidate],
    ) -> str:
        return PROMPT_TEMPLATE.format(
            max_hypotheses=self.settings.max_hypotheses,
            question=question,
            solution_text=_squeeze(solution_text, 2000) or "(なし)",
            steps_block=format_steps_block(steps),
            candidates_block=format_candidates_block(candidates),
        )

    def extract(
        self,
        question: str,
        steps: Iterable[Step],
        solution_text: str = "",
        *,
        retries: int = 2,
    ) -> ExtractionResult:
        steps = list(steps)
        candidates = build_candidates(steps)
        prompt = self.build_prompt(question, solution_text, steps, candidates)

        last = ExtractionResult(candidates=candidates)
        for attempt in range(1, retries + 1):
            raw = self._invoke(prompt)
            last = parse_response(raw, candidates)
            if last.ok:
                if last.unknown_eids:
                    log.warning("未知の eid を破棄しました: %s", sorted(set(last.unknown_eids)))
                return last
            log.warning("仮説抽出に失敗 (%s/%s): %s", attempt, retries, last.parse_error)
        return last

    def _invoke(self, prompt: str) -> str:
        response = self.llm.invoke(_as_messages(prompt))
        content = getattr(response, "content", response)
        if isinstance(content, list):
            return "\n".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        return str(content)


def _as_messages(prompt: str) -> Any:
    """langchain が無い環境（テスト・軽量 CI）でも動くようにフォールバックする。"""
    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        return [{"role": "user", "content": prompt}]
    return [HumanMessage(content=prompt)]
