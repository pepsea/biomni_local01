"""停止（キャンセル）.

停止が効かない状態には 2 つの原因があった。どちらも回帰させたくない。

1. proc.terminate() は直接の子にしか届かない。biomni は run_bash_script などで
   さらに別プロセスを起こすので、孫が残る。
2. 親側が multiprocessing.Queue をタイムアウト無しで待っていたため、子が
   _eof を送らずに死ぬと永久に待ち続け、ランが "running" のまま残って
   次のランを受け付けられなくなる。
"""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import time

import pytest

from backend.app.worker import terminate_tree


def _child_with_grandchild(ready_path: str) -> None:
    """孫プロセスを起こしてから待ち続ける子。worker.run_in_subprocess の縮小版。"""
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass
    proc = subprocess.Popen(["sleep", "300"])
    with open(ready_path, "w") as f:
        f.write(str(proc.pid))
    time.sleep(300)


def _alive(pid: int) -> bool:
    """シグナル 0 で存在確認する（ゾンビは残らない前提）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX 以外ではプロセスグループを使えない")
def test_terminate_tree_kills_grandchildren(tmp_path):
    ready = tmp_path / "ready"
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_child_with_grandchild, args=(str(ready),), daemon=True)
    proc.start()

    grandchild = 0
    try:
        for _ in range(200):          # 孫が立つのを待つ
            if ready.exists() and ready.read_text().strip():
                grandchild = int(ready.read_text().strip())
                break
            time.sleep(0.05)
        assert grandchild and _alive(grandchild), "孫プロセスが起動しなかった（前提が崩れている）"

        terminate_tree(proc, grace=5.0)

        assert not proc.is_alive(), "子プロセスが止まっていない"
        for _ in range(60):
            if not _alive(grandchild):
                break
            time.sleep(0.05)
        assert not _alive(grandchild), "孫プロセスが残っている（proc.terminate() だけでは届かない）"
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
        if grandchild:
            try:
                os.kill(grandchild, 9)
            except (ProcessLookupError, PermissionError):
                pass


def test_terminate_tree_is_safe_on_a_dead_process():
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=time.sleep, args=(0,), daemon=True)
    proc.start()
    proc.join(timeout=10)
    terminate_tree(proc)          # 例外を出さないこと
    terminate_tree(None)          # None も許容する


def _plain_child() -> None:
    """setsid を呼ばない子。start() 直後の子はしばらくこの状態にある。"""
    time.sleep(300)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX 以外ではプロセスグループを使えない")
def test_terminate_tree_never_signals_our_own_process_group():
    """子がまだ親と同じプロセスグループに居るとき、自分を撃たないこと。

    実測した壊れ方: 子は run_in_subprocess の先頭で setsid するが、spawn した
    Python が起動し切るまでの数秒はまだ親と同じグループに居る。その間に
    killpg すると親ごと死ぬ。pytest ではシェルに `Terminated` だけが残り、
    出力はパイプのバッファごと消える。Web アプリでは「停止」でサーバが落ちる。
    """
    import signal as signal_mod

    received: list[int] = []
    previous = signal_mod.signal(signal_mod.SIGTERM, lambda *_: received.append(1))
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_plain_child, daemon=True)
    proc.start()
    try:
        for _ in range(200):      # 子が同じグループに居るうちに撃ちたい
            if proc.is_alive() and os.getpgid(proc.pid) == os.getpgid(0):
                break
            time.sleep(0.05)

        terminate_tree(proc, grace=5.0)

        assert not received, "自分のプロセスグループに SIGTERM を撃っている"
        assert not proc.is_alive(), "グループを撃てない場合でも子は止めること"
    finally:
        signal_mod.signal(signal_mod.SIGTERM, previous)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
