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
from dataclasses import dataclass, field
from typing import Any

from biomni_hypo.agent_factory import AgentBundle, reset_agent_state
from biomni_hypo.citations import extract_citations
from biomni_hypo.guard import PolicyGuard, policy_guard
from biomni_hypo.schemas import Artifact, PlanItem, Step, StepKind, ToolCall

log = logging.getLogger(__name__)

EXECUTE_RE = re.compile(r"<execute>(.*?)</execute>", re.DOTALL)
#: 観測が「こちらの書いたコードの誤り」であることを示す形。
#: 外部 API の障害と混ぜてはいけない。混ぜると「データが無い」と
#: 結論してしまう（実測で踏んだ: docs/design/42）
CLIENT_ERROR_MARKS = (
    "unhashable type",
    "indices must be integers",
    "is not defined",
    "unexpected keyword argument",
    "cannot import name",
    "expected an indented block",
    "IndentationError",
    "SyntaxError",
    "invalid syntax",
    "has no attribute",
    "object is not subscriptable",
    "object is not iterable",
)


def is_client_error(observation: str) -> bool:
    """その観測は、外部の失敗ではなく、こちらのコードの誤りか。"""
    text = observation or ""
    if not text.lstrip().startswith("Error:"):
        return False
    if any(mark in text for mark in CLIENT_ERROR_MARKS):
        return True
    # 素の KeyError（`Error: 'results'` だけ）もこちら側の誤り
    return bool(re.match(r"^Error:\s*'[^']{1,60}'\s*$", text.strip()))


OBSERVATION_RE = re.compile(r"<observation>(.*?)</observation>", re.DOTALL)
SOLUTION_RE = re.compile(r"<solution>(.*?)</solution>", re.DOTALL)
#: コード中のデータファイル参照
FILE_REF_RE = re.compile(r"""['"]([\w./-]+\.(?:csv|tsv|parquet|pkl|json|obo|txt|h5ad|xlsx))['"]""")

# biomni が「タグが無い」応答を検知したときに会話へ差し込む定型文（a1.py の generate ノード）。
# これは LLM の思考ではなくフレームワークの差し戻しなので、think として出すと
# 「モデルが何か考えている」ように見えてしまう。専用の種別に分ける。
# biomni は「まず計画を立てろ」と指示している（a1.py の system prompt）:
#   "Given a task, make a plan first. ... Format your plan as a checklist
#    with empty checkboxes like this:  1. [ ] First step"
#   "Always show the updated plan after each step so the user can track progress."
# 実際そのとおり出てくるが、素通しすると単なる think になって埋もれる。
# ここで拾って「解析の設計」として独立させる（docs/design/19）。
PLAN_LINE_RE = re.compile(
    r"^\s*(?:\d+[.)]|[-*])\s*\[\s*([^\]]?)\s*\]\s*(.+?)\s*$",
    re.MULTILINE,
)
#: 計画とみなす最小行数。1 行だけの箇条書きを計画と呼ばない
MIN_PLAN_ITEMS = 2
#: 完了を表す印。モデルによって揺れる
_DONE_MARKS = {"✓", "✔", "x", "X", "√", "☑", "*"}
_FAILED_MARKS = {"✗", "✘", "×", "-", "!"}
#: "(failed because ...)" のような補足
_NOTE_RE = re.compile(r"[（(]\s*(?:failed|skipped|完了|失敗)?[^)）]*[)）]\s*$", re.IGNORECASE)

PARSE_RETRY_MARK = "there are no tags in the current response"
PARSE_GIVEUP_MARK = "execution terminated due to repeated parsing errors"

#: 連続で何回タグ無しが続いたら、こちらでランを打ち切るか。
#:
#: biomni にも打ち切り（2 回）が書いてあるが、**一度も発動しない**:
#:   - 差し戻しは HumanMessage として積まれるのに、条件は AIMessage しか数えない
#:   - 文言は "But there are no tags"（小文字 t）だが、条件は
#:     "There are no tags"（大文字 T）を探している
#: 実測で 28 回まで回っていた。止める側が誰もいない（docs/design/23）。
#:
#: 1〜2 回は持ち直すことがあるので（§16）、**連続** 3 回で打ち切る。
MAX_CONSECUTIVE_PARSE_ERRORS = 3

#: 差し戻しが起きたときに UI・ログへ出す原因と対処
#: 手掛かりが何も無いときの一般形。可能なら parse_error_hint() を使うこと。
PARSE_ERROR_HINT = (
    "モデルが <execute> / <solution> のどちらも出力しませんでした。"
    "よくある原因: (1) context が尽きてタグの規定が落ちている"
    "（docs/design/22。実行設定の num_ctx と「約 N 手で埋まります」の警告を確認）、"
    "(2) num_predict が小さく <think> の途中で生成が尽きる、"
    "(3) 指示追従性の低いモデル。より大きな num_ctx / num_predict か、"
    "別のモデル（qwen3:14b 以上、または Claude）を試してください。"
)

#: 「まだ何も積まれていない」とみなすステップ数。ここでの失敗に context は無関係
_EARLY_STEPS = 3


def parse_error_hint(bundle: Any, step_idx: int) -> str:
    """差し戻しの理由を、その場で測って書く。

    実測: ステップ 0 での差し戻しに「context が尽きて」と出していた。
    0 手目にはまだ何も積まれていないので、それはあり得ない。
    原因の一覧を並べるのをやめ、**測れるものは測って**言うこと
    （docs/design/45）。
    """
    settings = getattr(bundle, "settings", None)
    num_ctx = getattr(settings, "num_ctx", 0) or 0
    prompt_tokens = getattr(bundle, "estimated_prompt_tokens", 0) or 0
    num_predict = getattr(settings, "num_predict", 0) or 0
    model = getattr(settings, "model", "") or "（不明）"

    if not num_ctx:
        return PARSE_ERROR_HINT

    # 1. そもそもプロンプトが入りきっていないか
    if prompt_tokens and prompt_tokens >= num_ctx * 0.8:
        return (
            f"モデルが <execute> / <solution> のどちらも出力しませんでした。"
            f"システムプロンプトだけで num_ctx の {prompt_tokens / num_ctx:.0%} を"
            f"占めています（約 {prompt_tokens:,} / {num_ctx:,} トークン）。"
            f"**最初から入りきっていません。** num_ctx を上げるか、"
            f"ツールモジュールを絞ってください（docs/design/22）。"
        )

    # 2. 早い段階の失敗に context は関係ない
    if step_idx < _EARLY_STEPS:
        # 使用量が分からないときに「約 0 トークン」と書かない
        usage = (
            f"（まだ約 {prompt_tokens:,} / {num_ctx:,} トークンしか使っていません）"
            if prompt_tokens
            else "（まだ何も積まれていません）"
        )
        return (
            "モデルが <execute> / <solution> のどちらも出力しませんでした。"
            f"ステップ {step_idx} での失敗なので、**context 切れではありません**{usage}。"
            f"指示追従性の問題です。num_predict={num_predict:,} が小さいと "
            "<think> の途中で生成が尽きることもあります。"
            # いま使っているモデルを勧め返さないこと（qwen3:14b で動かして
            # 「qwen3:14b 以上を試せ」は助言になっていない）
            "より指示追従性の高いモデルに替えると直ることが多いです"
            f"（現在: {model}）。"
        )

    # 3. 後半なら、あと何手で埋まるかを添える
    from biomni_hypo.models import TOKENS_PER_STEP, estimate_steps_until_full

    budget = estimate_steps_until_full(num_ctx, prompt_tokens)
    used = prompt_tokens + step_idx * TOKENS_PER_STEP
    return (
        f"モデルが <execute> / <solution> のどちらも出力しませんでした。"
        f"ステップ {step_idx} 時点で約 {used:,} / {num_ctx:,} トークンを使っています"
        f"（1 手あたり約 {TOKENS_PER_STEP:,}、埋まるまで約 {budget} 手）。"
        f"context が尽きてタグの規定が落ちている可能性があります（docs/design/22）。"
        f"num_ctx を上げるか、より大きなモデルを試してください。現在: {model}"
    )


@dataclass
class TraceResult:
    steps: list[Step]
    solution_text: str
    resources_considered: dict[str, list[str]]
    #: 最新の解析計画
    plan: list[PlanItem] = field(default_factory=list)
    #: 計画が書き直された回数（初回を除く）
    plan_revisions: int = 0
    stopped_reason: str = ""
    #: LLM の生出力に <observation> が現れた回数。0 でなければ stop が効いていない（AC-1）
    hallucinated_observations: int = 0
    #: 実行したコード側の誤りで失敗した観測の数。外部の障害と分けて数える
    client_errors: int = 0
    #: biomni がタグ無し応答を差し戻した回数
    parsing_errors: int = 0
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
        self.plan: list[PlanItem] = []
        self.plan_revisions = 0
        self.stopped_reason = ""
        self.hallucinated_observations = 0
        self.client_errors = 0
        self.parsing_errors = 0
        self.consecutive_parse_errors = 0
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

                if self.consecutive_parse_errors >= MAX_CONSECUTIVE_PARSE_ERRORS:
                    # biomni の打ち切りは効かない（§23）ので、こちらで抜ける。
                    # 放っておくと recursion_limit=500 まで同じ失敗を繰り返す
                    break
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
            plan=list(self.plan),
            plan_revisions=self.plan_revisions,
            stopped_reason=self.stopped_reason,
            hallucinated_observations=self.hallucinated_observations,
            client_errors=self.client_errors,
            parsing_errors=self.parsing_errors,
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

        parse_step = self._classify_parse_error(text, idx, duration_ms)
        if parse_step is not None:
            return [parse_step]
        if EXECUTE_RE.search(text) or SOLUTION_RE.search(text):
            # タグ付きで返せた = 持ち直した。連続カウントは戻す。
            # 通算（parsing_errors）は戻さない。何回つまずいたかは残す
            self.consecutive_parse_errors = 0

        obs = OBSERVATION_RE.search(text)
        exe = EXECUTE_RE.search(text)
        sol = SOLUTION_RE.search(text)

        # observation は execute ノードが差し込んだもの。
        # ただし LLM 側が生成してしまった場合（stop が効いていない）はここで検知する。
        if obs and not exe:
            observation = obs.group(1).strip()
            blocked = observation.startswith("POLICY BLOCKED")
            if is_client_error(observation):
                self.client_errors += 1
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
            idx = self._emit_preamble(out, text[: exe.start()], idx, duration_ms)
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
            # 最終ターンでも計画は更新される（biomni は毎ターン再掲させる）。
            # ここを飛ばすと「最後に何が終わって何が失敗したか」が残らない
            idx = self._emit_preamble(out, text[: sol.start()], idx, duration_ms)
            self.solution_text = sol.group(1).strip()
            out.append(Step(idx=idx, kind=StepKind.SOLUTION, text=self.solution_text, duration_ms=duration_ms))
            return out

        plan_step = self._plan_step(text, idx, duration_ms)
        if plan_step is not None:
            out.append(plan_step)
            idx += 1
        rest = _strip_plan(text)
        if rest or not out:
            out.append(
                Step(idx=idx, kind=StepKind.THINK, text=_strip_tags(rest or text), duration_ms=duration_ms)
            )
        return out

    def _emit_preamble(self, out: list[Step], raw: str, idx: int, duration_ms: int) -> int:
        """タグの手前にある文章を、計画と思考に分けて積む。

        計画は think に混ぜない。混ぜると「解析の設計」が思考の断片として
        埋もれる（docs/design/19 §19.3）。
        """
        preamble = raw.strip()
        if not preamble:
            return idx
        plan_step = self._plan_step(preamble, idx, duration_ms)
        if plan_step is not None:
            out.append(plan_step)
            idx += 1
        rest = _strip_plan(preamble)
        if rest:
            out.append(Step(idx=idx, kind=StepKind.THINK, text=_strip_tags(rest)))
            idx += 1
        return idx

    def _plan_step(self, text: str, idx: int, duration_ms: int) -> Step | None:
        """チェックリスト形式の計画を拾って PLAN ステップにする。

        biomni は毎ターン計画を再掲させるので、同じ内容が何度も流れてくる。
        中身が変わったときだけ「書き直し」として数える。
        """
        items = parse_plan(text)
        if len(items) < MIN_PLAN_ITEMS:
            return None
        changed = [(i.text, i.state) for i in items] != [(i.text, i.state) for i in self.plan]
        if self.plan and changed:
            # 手順の並び自体が変わった場合だけ「書き直し」。
            # チェックが進んだだけなら進捗であって書き直しではない
            if [i.text for i in items] != [i.text for i in self.plan]:
                self.plan_revisions += 1
        self.plan = items
        if not changed:
            return None  # 同じ計画の再掲。ステップにしない
        done = sum(1 for i in items if i.state == "done")
        return Step(
            idx=idx,
            kind=StepKind.PLAN,
            text=f"解析の計画（{done}/{len(items)} 完了）",
            plan=items,
            duration_ms=duration_ms,
        )

    def _classify_parse_error(self, text: str, idx: int, duration_ms: int) -> Step | None:
        """biomni の差し戻し／打ち切りメッセージなら PARSING_ERROR にする。

        これを think に混ぜると、画面には「0 think ＜英語の叱責文＞」とだけ出て、
        何が起きたのか（モデルがタグを出せていない）が読み取れない。
        """
        low = text.lower()
        if PARSE_GIVEUP_MARK in low:
            self.parsing_errors += 1
            self.stopped_reason = (
                "モデルがタグ付きの応答を出せず、biomni が打ち切りました。"
                + parse_error_hint(self.bundle, idx)
            )
            log.error("biomni が解析エラーでランを打ち切りました。%s", self.stopped_reason)
            return Step(
                idx=idx,
                kind=StepKind.PARSING_ERROR,
                text=self.stopped_reason,
                error=text.strip(),
                duration_ms=duration_ms,
            )
        if PARSE_RETRY_MARK in low:
            self.parsing_errors += 1
            self.consecutive_parse_errors += 1
            log.warning(
                "モデルがタグ無しで応答したため biomni が差し戻しました"
                "（通算 %d 回 / 連続 %d 回）。%s",
                self.parsing_errors,
                self.consecutive_parse_errors,
                parse_error_hint(self.bundle, idx),
            )
            note = (
                f"タグの無い応答を biomni が差し戻しました"
                f"（通算 {self.parsing_errors} 回 / 連続 {self.consecutive_parse_errors} 回）。"
            )
            if self.consecutive_parse_errors >= MAX_CONSECUTIVE_PARSE_ERRORS:
                # biomni の打ち切りは効かない。こちらで止める
                self.stopped_reason = (
                    f"タグの無い応答が {self.consecutive_parse_errors} 回続いたため打ち切りました。"
                    + parse_error_hint(self.bundle, idx)
                )
                note = self.stopped_reason
            return Step(
                idx=idx,
                kind=StepKind.PARSING_ERROR,
                text=note + ("" if note is self.stopped_reason else parse_error_hint(self.bundle, idx)),
                error=text.strip(),
                duration_ms=duration_ms,
            )
        return None

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


def parse_plan(text: str) -> list[PlanItem]:
    """チェックリスト形式の計画を PlanItem に変換する。

    biomni が指示している形:
        1. [ ] First step
        2. [✓] Second step (completed)
        3. [✗] Third step (failed because ...)

    印はモデルによって揺れる（x / X / ✔ / × / - など）ので広めに受ける。
    """
    items: list[PlanItem] = []
    for mark, body in PLAN_LINE_RE.findall(text):
        body = body.strip()
        if not body:
            continue
        m = (mark or "").strip()
        if m in _FAILED_MARKS:
            state = "failed"
        elif m and m in _DONE_MARKS:
            state = "done"
        else:
            state = "todo"
        note = ""
        found = _NOTE_RE.search(body)
        if found and state != "todo":
            note = found.group(0).strip("（）() ")
            body = body[: found.start()].strip()
        items.append(PlanItem(text=body[:300], state=state, note=note[:300]))
    return items


def _strip_plan(text: str) -> str:
    """計画の行を落とした残り（本文としての思考）。"""
    return PLAN_LINE_RE.sub("", text).strip()


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
