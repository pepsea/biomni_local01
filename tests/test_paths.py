"""置き場所の診断.

実際に踏んだバグ:
  診断のための関数が、診断の途中で PermissionError を投げて落ちた。
  Path.exists() は、途中の親を辿れないとそこで例外を出す。
  理由を出すための関数が落ちると、元の理由まで失われる。
"""

import os
from pathlib import Path

import pytest

from biomni_hypo.paths import (
    PathUnusable,
    describe_unusable,
    ensure_writable_dir,
    existing_ancestor,
    is_read_only,
    writable_candidate,
)


def test_a_writable_place_is_created_and_returned(tmp_path):
    target = ensure_writable_dir(tmp_path / "a" / "b", what="置き場")
    assert target.is_dir()


def test_an_unusable_place_raises_with_a_reason(tmp_path):
    with pytest.raises(PathUnusable) as got:
        ensure_writable_dir("/proc/nowhere/x", what="置き場", env_var="BIOMNI_PATH")
    text = str(got.value)
    assert "置き場を作れません" in text
    assert "実在する一番近い親" in text


def test_the_nearest_ancestor_survives_an_unreadable_parent(tmp_path, monkeypatch):
    """辿れない親があっても落ちないこと（診断が診断中に落ちてはいけない）。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    real_exists = Path.exists

    def picky(self):
        if str(self).startswith(str(blocked)):
            raise PermissionError(13, "Permission denied")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", picky)
    assert existing_ancestor(blocked / "deep" / "x") == tmp_path


def test_describe_never_raises(monkeypatch):
    """調べる途中で何が起きても、文字列を返すこと。"""
    def boom(_):
        raise RuntimeError("調査に失敗")

    monkeypatch.setattr("biomni_hypo.paths.existing_ancestor", boom)
    text = describe_unusable(Path("/nowhere"), OSError("元の理由"))
    assert "元の理由" in text
    assert "調べる途中でも失敗" in text


def test_a_uid_mismatch_is_named(tmp_path, monkeypatch):
    """書けない原因が所有者の食い違いなら、権限ではなく UID だと言うこと。"""
    monkeypatch.setattr("biomni_hypo.paths._can_write", lambda _p: False)
    monkeypatch.setattr(os, "getuid", lambda: 4242)

    text = describe_unusable(tmp_path, PermissionError("denied"), env_var="BIOMNI_PATH")
    assert "所有者が違います" in text
    assert "uid=4242" in text


def test_the_container_case_says_app_uid(tmp_path, monkeypatch):
    """コンテナでは chmod では直らない。APP_UID を合わせる手順を出す。"""
    monkeypatch.setattr("biomni_hypo.paths._can_write", lambda _p: False)
    monkeypatch.setattr("biomni_hypo.paths.in_container", lambda: True)
    monkeypatch.setattr(os, "getuid", lambda: 1000)

    text = describe_unusable(tmp_path, PermissionError("denied"))
    assert "APP_UID" in text
    assert "chown" not in text, "コンテナでは chown を勧めない"


def test_read_only_mounts_are_detected(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\nsrv:/v /mnt/storage nfs4 ro,relatime 0 0\n",
        encoding="utf-8",
    )
    assert is_read_only(Path("/mnt/storage/x"), mounts) is True
    assert is_read_only(Path("/home"), mounts) is False


def test_the_suggested_place_is_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    found = writable_candidate("probe-data")
    assert found == str(tmp_path / "probe-data")
    assert Path(found).is_dir()
