"""A1 の LangGraph を直接ストリームして、根拠の原材料を構造化する.

docs/design/02-architecture.md §2.3。

A1.go_stream() を使わない理由: あれは pretty_print() を通した整形済み文字列しか
yield せず、根拠抽出に必要な情報（生コード・生 observation・メッセージ種別）が
失われる。ここでは agent.app.stream() を直接回す。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from biomni_hypo.agent_factory import AgentBundle, reset_agent_state
from biomni_hypo.citations import extract_citations
from biomni_hypo.guard import PolicyGuard, policy_guard
from biomni_hypo.schemas import Artifact, Step, StepKind, ToolCall

log = logging.getLogger(__name__)

EXECUTE_RE = re.compile(r"<execute>(.*?)</execute>", re.DOTALL)
OBSERVATION_RE = re.compile(r"<observation>(.*?)</observation>", re.DOTALL)
SOLUTION_RE = re.compile(r"<solution>(.*?)</solution>", re.DOTALL)
#: コード中のデータファイル参照
FILE_REF_RE = re.compile(r"""['"]([\w./-]+\.(?:csv|tsv|parquet|pkl|json|obo|txt|h5ad|xlsx))['"]""")


@dataclass
class TraceResult:
    steps: list[Step]
    solution_text: str
    resources_considered: dict[str, list[str]]
    stopped_reason: str = ""
    #: LLM の生出力に <observation> が現れた回数。0 でなければ stop が効いていない（AC-1）
    hallucinated_observations: int = 0
    #: 実況で流したトークン数
    streamed_tokens: int = 0


class TracingRunner:
    """1 ランを実行し、Step を逐次 yield する。

    ノートブックからは `for step in runner.iter_steps(q): ...`
    Web ワーカーからは同じループを回して SSE に流す。
    """

    def __init__(
        self,
        bundle: AgentBundle,
        run_id: str | None = None,
        *,
        guard_module: Any = None,
    ) -> None:
        self.bundle = bundle
        #: ポリシーガードの差し替え対象（既定 biomni.agent.a1）。テスト用の穴。
        self.guard_module = guard_module
        self.agent = bundle.agent
        self.policy = bundle.policy
        self.run_id = run_id or f"r_{uuid.uuid4().hex[:12]}"
        self.steps: list[Step] = []
        self.solution_text = ""
        self.resources_considered: dict[str, list[str]] = {}
        self.stopped_reason = ""
        self.hallucinated_observations = 0
        self.streamed_tokens = 0

    # ------------------------------------------------------------------ 実行

    def iter_steps(
        self,
        question: str,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Iterator[Step]:
        """ステップを逐次返す。生成器を回しきるとランが完了する。"""
        settings = self.bundle.settings
        reset_agent_state(self.agent)
        self.agent.user_task = question
        self.steps.clear()
        started = time.monotonic()

        if settings.use_tool_retriever:
            self.resources_considered = self._select_resources(question)
            if on_event:
                on_event("resources_selected", self.resources_considered)

        # 生成トークンの実況。A1 は invoke() を同期で呼ぶが、ChatOllama は内部で
        # ストリーミングしているので、コールバック経由でトークンが取れる。
        self._attach_token_stream(on_event)

        inputs = {"messages": [_human_message(question)], "next_step": None}
        config = {
            "recursion_limit": 500,
            # A1.go() は thread_id を 42 に固定する。ラン間で状態が混ざらないよう分ける。
            "configurable": {"thread_id": self.run_id},
        }

        with policy_guard(self.policy, self.guard_module) as guard:
            # 入力として渡した質問メッセージはステップに数えない
            seen = len(inputs["messages"])
            last_ts = time.monotonic()
            for state in self.agent.app.stream(inputs, stream_mode="values", config=config):
                messages = state.get("messages", [])
                if len(messages) <= seen:
                    continue
                for msg in messages[seen:]:
                    now = time.monotonic()
                    for step in self._classify(msg, guard, int((now - last_ts) * 1000)):
                        self.steps.append(step)
                        if on_event:
                            on_event("step", step.model_dump(mode="json"))
                        yield step
                    last_ts = now
                seen = len(messages)

                if len(self.steps) >= settings.max_steps:
                    self.stopped_reason = f"max_steps({settings.max_steps}) に到達"
                    break
                if time.monotonic() - started > settings.wallclock_limit_sec:
                    self.stopped_reason = f"wallclock_limit({settings.wallclock_limit_sec}s) に到達"
                    break

        self._detach_token_stream()
        self._attach_artifacts()
        if on_event:
            on_event("trace_done", {"stopped_reason": self.stopped_reason})

    def _attach_token_stream(self, on_event: Callable[[str, dict[str, Any]], None] | None) -> None:
        handler = getattr(self.bundle, "token_stream", None)
        if handler is None or on_event is None:
            return

        def sink(kind: str, text: str) -> None:
            if kind == "token":
                self.streamed_tokens += 1
            on_event("token", {"kind": kind, "text": text})

        handler.sink = sink

    def _detach_token_stream(self) -> None:
        handler = getattr(self.bundle, "token_stream", None)
        if handler is not None:
            handler.sink = None

    def run(self, question: str, **kwargs: Any) -> TraceResult:
        """iter_steps を回しきって結果をまとめて返す（ノートブック向け）。"""
        for _ in self.iter_steps(question, **kwargs):
            pass
        return self.result()

    def result(self) -> TraceResult:
        return TraceResult(
            steps=list(self.steps),
            solution_text=self.solution_text,
            resources_considered=dict(self.resources_considered),
            stopped_reason=self.stopped_reason,
            hallucinated_observations=self.hallucinated_observations,
            streamed_tokens=self.streamed_tokens,
        )

    # ------------------------------------------------------------------ 分類

    def _classify(self, msg: Any, guard: PolicyGuard, duration_ms: int) -> list[Step]:
        """1 メッセージを Step に分解する。

        1 つのメッセージが <think> と <execute> の両方を含むことがあるので list を返す。
        """
        text = _content_of(msg)
        if not text or not text.strip():
            return []

        out: list[Step] = []
        idx = len(self.steps)

        obs = OBSERVATION_RE.search(text)
        exe = EXECUTE_RE.search(text)
        sol = SOLUTION_RE.search(text)

        # observation は execute ノードが差し込んだもの。
        # ただし LLM 側が生成してしまった場合（stop が効いていない）はここで検知する。
        if obs and not exe:
            observation = obs.group(1).strip()
            blocked = observation.startswith("POLICY BLOCKED")
            prev_tools = self.steps[-1].tools if self.steps else []
            citations = extract_citations(observation, step_idx=idx, tools_in_step=prev_tools)
            out.append(
                Step(
                    idx=idx,
                    kind=StepKind.POLICY_BLOCKED if blocked else StepKind.OBSERVATION,
                    text=observation,
                    citations=citations,
                    duration_ms=duration_ms,
                )
            )
            return out

        if exe:
            preamble = text[: exe.start()].strip()
            if preamble:
                out.append(Step(idx=idx, kind=StepKind.THINK, text=_strip_tags(preamble)))
                idx += 1
            code = exe.group(1).strip()
            if OBSERVATION_RE.search(text[exe.end():]):
                # </execute> の先に自分で observation を書いている = stop が効いていない
                self.hallucinated_observations += 1
                log.error(
                    "LLM が <observation> を自己生成しました。stop シーケンスが効いていません "
                    "(docs/design/04 §4.1)。"
                )
            out.append(
                Step(
                    idx=idx,
                    kind=StepKind.EXECUTE,
                    code=code,
                    tools=self._tools_in_code(code),
                    datasets=self._datasets_in_code(code),
                    user_files=self._user_files_in_code(code),
                    duration_ms=duration_ms,
                )
            )
            return out

        if sol:
            self.solution_text = sol.group(1).strip()
            out.append(Step(idx=idx, kind=StepKind.SOLUTION, text=self.solution_text, duration_ms=duration_ms))
            return out

        out.append(Step(idx=idx, kind=StepKind.THINK, text=_strip_tags(text), duration_ms=duration_ms))
        return out

    # -------------------------------------------------------------- 抽出補助

    def _tools_in_code(self, code: str) -> list[ToolCall]:
        try:
            pairs = self.agent._parse_tool_calls_with_modules(code)
        except Exception as exc:  # noqa: BLE001 - 解析失敗でランを落とさない
            log.warning("ツール解析に失敗: %s", exc)
            return []
        out: list[ToolCall] = []
        for item in pairs or []:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                out.append(ToolCall(name=str(item[0]), module=str(item[1])))
            else:
                out.append(ToolCall(name=str(item)))
        return out

    def _datasets_in_code(self, code: str) -> list[str]:
        """コードが実際に触れたデータレイクのファイル名（3 段階の B）。"""
        known = set(getattr(self.agent, "data_lake_dict", {}) or {})
        found = {m.group(1).rsplit("/", 1)[-1] for m in FILE_REF_RE.finditer(code)}
        return sorted(f for f in found if f in known)

    def _user_files_in_code(self, code: str) -> list[str]:
        known = set(getattr(self.agent, "data_lake_dict", {}) or {})
        custom = set(getattr(self.agent, "_custom_data", {}) or {})
        found = {m.group(1).rsplit("/", 1)[-1] for m in FILE_REF_RE.finditer(code)}
        return sorted(f for f in found if f in custom or (f not in known and f in custom))

    def _select_resources(self, question: str) -> dict[str, list[str]]:
        """リソース検索フェーズ（3 段階の A: 検討対象）。

        全カテゴリが空で返ってきたら、検索が機能していない可能性が高い
        （docs/design/04 §4.5）。呼び出し側が気付けるよう警告を出す。
        """
        try:
            selected = self.agent._prepare_resources_for_retrieval(question)
        except Exception as exc:  # noqa: BLE001
            log.warning("リソース検索に失敗: %s", exc)
            return {}
        if not selected:
            return {}
        self.agent.update_system_prompt_with_selected_resources(selected)
        out = {
            "tools": [_name_of(t) for t in selected.get("tools", [])],
            "datasets": [_name_of(d) for d in selected.get("data_lake", [])],
            "libraries": [_name_of(x) for x in selected.get("libraries", [])],
            "know_how": [_name_of(x) for x in selected.get("know_how", [])],
        }
        if not any(out.values()):
            log.warning(
                "リソース検索が全カテゴリ空を返しました。num_ctx 不足の可能性があります "
                "(docs/design/04 §4.5)。"
            )
        return out

    def _attach_artifacts(self) -> None:
        """実行中に生成された図を、対応する execute ステップに紐付ける。"""
        results = getattr(self.agent, "_execution_results", []) or []
        exec_steps = [s for s in self.steps if s.kind == StepKind.EXECUTE]
        # 実行結果とステップは同数とは限らない（打ち切り時など）ので短い方に合わせる
        for entry, step in zip(results, exec_steps, strict=False):
            for i, img in enumerate(entry.get("images", []) or []):
                data = img.get("data") if isinstance(img, dict) else img
                step.artifacts.append(
                    Artifact(id=f"{step.idx}_{i}", kind="image", mime="image/png", data_b64=str(data))
                )


def _human_message(text: str) -> Any:
    """langchain が無い環境でも TracingRunner を組み立てられるようにする。"""
    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        return {"role": "user", "content": text}
    return HumanMessage(content=text)


def _content_of(msg: Any) -> str:
    content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
    return content if isinstance(content, str) else _flatten_content(content)


def _flatten_content(content: Any) -> str:
    """Responses API 形式（content ブロックのリスト）を文字列に潰す。"""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _strip_tags(text: str) -> str:
    return re.sub(r"</?(?:think|thinking|plan)>", "", text).strip()


def _name_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("id") or item)
    if isinstance(item, str) and ": " in item:
        return item.split(": ", 1)[0]
    return str(item)
