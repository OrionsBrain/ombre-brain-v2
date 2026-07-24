"""热更新来源白名单 + 自动 pip 安装闸门（安全加固 #2）。

do-update 会把远端 zip 覆盖到 src/ 并（旧行为）自动 pip install，等于把「谁能改
config.update」放大成 RCE。默认只信官方仓、自动 pip 默认关闭。
"""
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import web.meta as meta
import web._shared as sh


@pytest.fixture(autouse=True)
def _restore():
    old = sh.config
    yield
    sh.config = old


def test_official_repo_allowed(monkeypatch):
    monkeypatch.delenv("OMBRE_ALLOW_CUSTOM_UPDATE_REPO", raising=False)
    assert meta._update_repo_allowed("P0luz/Ombre-Brain")
    assert meta._update_repo_allowed("p0luz/ombre-brain")   # 大小写不敏感
    assert meta._update_repo_allowed("/P0luz/Ombre-Brain/")  # 容忍多余斜杠


def test_foreign_repo_rejected_by_default(monkeypatch):
    monkeypatch.delenv("OMBRE_ALLOW_CUSTOM_UPDATE_REPO", raising=False)
    assert not meta._update_repo_allowed("attacker/evil")
    assert not meta._update_repo_allowed("p0luz/ombre-brain-evil")


def test_foreign_repo_allowed_via_optin(monkeypatch):
    monkeypatch.setenv("OMBRE_ALLOW_CUSTOM_UPDATE_REPO", "1")
    assert meta._update_repo_allowed("myfork/ombre-brain")


def test_pip_install_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)
    monkeypatch.setattr(sh, "config", {})
    assert meta._pip_install_allowed() is False


def test_pip_install_enabled_via_config(monkeypatch):
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)
    monkeypatch.setattr(sh, "config", {"update": {"allow_pip_install": True}})
    assert meta._pip_install_allowed() is True


def test_pip_install_enabled_via_env(monkeypatch):
    monkeypatch.setattr(sh, "config", {})
    monkeypatch.setenv("OMBRE_UPDATE_ALLOW_PIP", "1")
    assert meta._pip_install_allowed() is True


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        ("2.6.1", "v2.4.6", True),
        ("2.6.1", "2.6.1", False),
        ("2.6.1", "2.7.0", False),
        ("2.6", "2.6.0", False),
        ("dev", "2.4.6", False),
    ],
)
def test_hot_update_downgrade_guard(current, target, expected):
    assert meta._is_version_downgrade(current, target) is expected


def test_hot_update_defaults_to_same_main_branch_as_version_check():
    source = open(meta.__file__, encoding="utf-8").read()
    assert '_ucfg.get("channel") or "branch"' in source


def test_ci_lock_verification_freezes_package_index_snapshot():
    repo_root = Path(meta.__file__).resolve().parents[2]
    workflow = (repo_root / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )
    _, remainder = workflow.split("      - name: Verify dependency lockfiles", 1)
    step = remainder.split("\n      - name:", 1)[0]

    match = re.search(
        r"(?m)^\s+UV_EXCLUDE_NEWER:\s*['\"]([^'\"]+)['\"]\s*$",
        step,
    )
    assert match, "lock 校验必须固定包索引时间，避免无输入变更时发生解析漂移"
    cutoff_text = match.group(1)
    assert cutoff_text.endswith("Z"), "lock 索引时间必须使用明确的 UTC 时间"
    cutoff = datetime.fromisoformat(cutoff_text[:-1] + "+00:00")
    assert cutoff.tzinfo == timezone.utc
    assert cutoff <= datetime.now(timezone.utc)

    assert step.count("uv pip compile ") == 2
    assert "--upgrade" not in step
    assert "--exclude-newer" not in step, (
        "cutoff 必须通过环境变量传入，避免 uv 把参数写进 lock 头部造成纯文本漂移"
    )
    reset_command = "rm -f requirements.lock.txt requirements-dev.lock.txt"
    assert reset_command in step, "lock 校验必须从空输出重建，不能依赖已有 pin 偏好"
    assert step.index(reset_command) < step.index("uv pip compile ")


def test_release_archive_omits_loose_requirements_but_keeps_lock():
    repo_root = Path(meta.__file__).resolve().parents[2]
    attributes = (repo_root / ".gitattributes").read_text(encoding="utf-8")
    active_rules = {
        line.strip()
        for line in attributes.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "/requirements.txt export-ignore" in active_rules
    assert "/requirements.lock.txt export-ignore" not in active_rules
    assert "COPY requirements.lock.txt ./" in (repo_root / "Dockerfile").read_text(
        encoding="utf-8"
    )


def test_legacy_archive_compatibility_requires_284_release_lock():
    repo_root = Path(meta.__file__).resolve().parents[2]
    lock_bytes = (repo_root / "requirements.lock.txt").read_bytes()
    normalized = lock_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    assert hashlib.sha256(normalized).hexdigest() == (
        "fdb24053349d8e18a55c3a5afbab8b92cc31d94b69e52359b350ab96b79001c9"
    ), (
        "requirements.lock.txt 已变化：发布前必须先移除 requirements.txt 的 "
        "export-ignore 兼容规则，并为旧更新器设计显式依赖迁移"
    )


def test_dependency_check_uses_release_lock_and_normalizes_line_endings(tmp_path):
    old_source = b"mcp>=1.0.0\r\n"
    new_source = b"mcp>=1.27,<2\n"
    old_lock = b"mcp==1.28.1 \\\r\n    --hash=sha256:abc\r\n"
    new_lock = b"mcp==1.28.1 \\\n    --hash=sha256:abc\n"
    (tmp_path / "requirements.txt").write_bytes(old_source)
    (tmp_path / "requirements.lock.txt").write_bytes(old_lock)

    assert meta._requirements_changed(
        str(tmp_path), new_source, new_lock
    ) is False
    assert meta._requirements_changed(
        str(tmp_path), old_source, b"mcp==1.28.2\n"
    ) is True


def test_dependency_check_falls_back_to_source_for_legacy_archive(tmp_path):
    (tmp_path / "requirements.txt").write_bytes(b"mcp>=1.0.0\r\n")

    assert meta._requirements_changed(
        str(tmp_path), b"mcp>=1.0.0\n", None
    ) is False
    assert meta._requirements_changed(
        str(tmp_path), b"mcp>=1.27,<2\n", None
    ) is True


def test_dependency_check_falls_back_to_configured_image_root(
    monkeypatch, tmp_path
):
    runtime_root = tmp_path / "runtime"
    image_root = tmp_path / "image"
    runtime_root.mkdir()
    image_root.mkdir()
    (image_root / "requirements.lock.txt").write_bytes(b"mcp==1.28.1\r\n")
    monkeypatch.setenv("OMBRE_IMAGE_ROOT", str(image_root))

    assert meta._requirements_changed(
        str(runtime_root),
        b"mcp>=1.27,<2\n",
        b"mcp==1.28.1\n",
    ) is False


def test_dependency_check_without_any_baseline_remains_fail_closed(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OMBRE_IMAGE_ROOT", raising=False)
    monkeypatch.setattr(sh, "in_docker", lambda: False)

    assert meta._requirements_changed(
        str(tmp_path), b"new-package==1\n", b"new-package==1 --hash=sha256:abc\n"
    ) is True
    assert meta._requirements_changed(
        str(tmp_path), None, b"new-package==1 --hash=sha256:abc\n"
    ) is True


def test_new_lock_never_falls_back_to_matching_source_without_lock_baseline(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OMBRE_IMAGE_ROOT", raising=False)
    monkeypatch.setattr(sh, "in_docker", lambda: False)
    source = b"package>=1\n"
    (tmp_path / "requirements.txt").write_bytes(source)

    assert meta._requirements_changed(
        str(tmp_path), source, b"package==1 --hash=sha256:abc\n"
    ) is True


def test_lock_install_enforces_hashes(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "requirements.lock.txt"
    data = b"package==1 --hash=sha256:abc\n"

    result = meta._install_update_requirements(
        str(target), data, require_hashes=True
    )

    assert result.returncode == 0
    assert target.read_bytes() == data
    assert "--require-hashes" in captured["command"]
    assert captured["command"][-2:] == ["-r", str(target)]
