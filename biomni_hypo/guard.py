"""コード実行直前のポリシー検査（docs/design/05 §5.2 強制ポイント 3 / 最後の砦）.

biomni.agent.a1 は実行関数をモジュール名前空間に import している
（`from biomni.tool.support_tools import run_python_repl` など）ので、
その属性を差し替えることで「実行される直前」に割り込める。

ポリシー違反のコードは実行せず、違反内容を observation として返す。
エージェントはそれを読んで別の手段を探して続行する（ランを落とさない）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from biomni_hypo.policy import ResourcePolicy, Violation

log = logging.getLogger(__name__)

GUARDED_FUNCTIONS = ("run_python_repl", "run_bash_script", "run_r_code")


@dataclass
class BlockedExecution:
    code: str
    violations: list[Violation]

    def as_observation(self) -> str:
        lines = [
            "POLICY BLOCKED: このコードは商用利用ポリシーに違反するため実行されませんでした。",
            *[f"  - {v.as_message()}" for v in self.violations],
            "許可されたツール・データセットのみを使う別の方法を検討してください。",
        ]
        return "\n".join(lines)


@dataclass
class PolicyGuard:
    """ブロックした実行を記録する。トレースに `policy_blocked` ステップとして出す。"""

    policy: ResourcePolicy
    blocked: list[BlockedExecution] = field(default_factory=list)

    def check(self, code: str) -> BlockedExecution | None:
        violations = self.policy.inspect_code(code)
        if not violations:
            return None
        b = BlockedExecution(code=code, violations=violations)
        self.blocked.append(b)
        log.warning("policy blocked execution: %s", [v.as_message() for v in violations])
        return b

    def wrap(self, fn: Callable[..., str]) -> Callable[..., str]:
        def guarded(code: str, *args: Any, **kwargs: Any) -> str:
            blocked = self.check(code)
            if blocked is not None:
                return blocked.as_observation()
            return fn(code, *args, **kwargs)

        guarded.__name__ = getattr(fn, "__name__", "guarded")
        guarded.__doc__ = getattr(fn, "__doc__", None)
        return guarded

    def take_blocked(self) -> list[BlockedExecution]:
        """記録を取り出して空にする（ランごとにリセットする用）。"""
        out = list(self.blocked)
        self.blocked.clear()
        return out


@contextmanager
def policy_guard(policy: ResourcePolicy, module: Any = None) -> Iterator[PolicyGuard]:
    """実行関数を差し替えて、抜けたら必ず戻す。

    Args:
        module: 差し替え対象。既定は biomni.agent.a1。
            テストでは同名の関数を持つダミーモジュールを渡す。
    """
    if module is None:
        import biomni.agent.a1 as module  # 遅延 import

    a1 = module
    guard = PolicyGuard(policy)
    originals: dict[str, Callable[..., str]] = {}
    for name in GUARDED_FUNCTIONS:
        fn = getattr(a1, name, None)
        if fn is None:
            log.warning("biomni.agent.a1 に %s がありません。ガードをスキップします。", name)
            continue
        originals[name] = fn
        setattr(a1, name, guard.wrap(fn))
    try:
        yield guard
    finally:
        for name, fn in originals.items():
            setattr(a1, name, fn)
