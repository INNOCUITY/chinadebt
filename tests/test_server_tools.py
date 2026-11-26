"""服务器封装的安全传输、失败传播与隔离运行契约，不依赖 SSH/GPU。"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "server"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    loaded = []
    for name in ("ops", "run"):
        spec = importlib.util.spec_from_file_location(f"server_test_{name}", SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


def test_data_pack_roundtrip_and_tamper(modules, tmp_path):
    ops, _ = modules
    data = tmp_path / "original"
    (data / "processed").mkdir(parents=True)
    (data / "versions" / "v1").mkdir(parents=True)
    (data / "processed" / "curve.parquet").write_bytes(b"sample")
    (data / "versions" / "v1" / "funding.parquet").write_bytes(b"funding")
    archive = tmp_path / "data.tar.gz"
    ops.pack_data(data, archive)
    ops.unpack_data(archive, tmp_path / "received")
    assert ops.inventory(data) == ops.inventory(tmp_path / "received" / "data")
    assert archive.with_name(archive.name + ".sha256").exists()
    with pytest.raises(ValueError, match="拒绝覆盖"):
        ops.unpack_data(archive, tmp_path / "received")
    (tmp_path / "received" / "data" / "processed" / "curve.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        ops.verify_data_bundle(tmp_path / "received")


@pytest.mark.parametrize(
    "name,kind", [("../escape", "file"), ("data/link", "symlink"), ("/tmp/escape", "file")]
)
def test_unpack_rejects_unsafe_members(modules, tmp_path, name, kind):
    ops, _ = modules
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            out.addfile(member)
        else:
            member.size = 1
            out.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="非法"):
        ops.unpack_data(archive, tmp_path / "received")
    assert not (tmp_path / "received").exists()
    assert not (tmp_path / "escape").exists()


def test_bundle_does_not_include_raw_data_or_source(modules, tmp_path):
    ops, _ = modules
    for relative in (
        "work/data/raw.xlsx",
        "work/.git/config",
        "work/src/ficc.py",
        "reports/nav.csv",
        "logs/test.log",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    ops.write_json(tmp_path / "status.json", {"state": "failed"})
    archive = ops.pack_results(tmp_path)
    with tarfile.open(archive) as packed:
        names = packed.getnames()
        assert "reports/nav.csv" in names
        assert not any(name.startswith("work/") for name in names)
        manifest = json.load(packed.extractfile("BUNDLE_MANIFEST.json"))
        for name, expected in manifest["files"].items():
            import hashlib

            assert hashlib.sha256(packed.extractfile(name).read()).hexdigest() == expected


def make_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for directory in ("src", "scripts/server", "tests", "config", "reports", "data"):
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / ".gitkeep").touch()
    (root / ".gitignore").write_text("runs/\ndata/\n")
    (root / "README.md").write_text("fixture")
    (root / "scripts/server/verify_data.py").write_text("print('fixture verified')\n")
    (root / "scripts/run_local_example.py").write_text("raise SystemExit(7)\n")
    (root / "reports/old_report.csv").write_text("old result")
    (root / "reports/ml_dataset.csv").write_text("seed")
    (root / "data/frozen.bin").write_bytes(b"immutable")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    return root


@pytest.mark.parametrize("external_runs", [False, True])
def test_failed_step_is_packaged_and_original_not_modified(
    modules, tmp_path, monkeypatch, external_runs
):
    _, runner = modules
    root = make_repo(tmp_path)
    runs = tmp_path / "data_disk" / "runs" if external_runs else root / "runs"
    monkeypatch.delenv("FICC_RUNS_DIR", raising=False)
    if external_runs:
        monkeypatch.setenv("FICC_RUNS_DIR", str(runs))
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "environment", lambda: {"fixture": True})
    original = subprocess.check_output

    def output(command, **kwargs):
        if command[1:4] == ["-m", "pip", "freeze"]:
            return "fixture==1\n"
        return original(command, **kwargs)

    monkeypatch.setattr(runner.subprocess, "check_output", output)
    assert runner.main(["smoke"]) == 1
    run = next(runs.iterdir())
    if external_runs:
        assert not (root / "runs").exists()
    status = json.loads((run / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["steps"][-1]["exit_code"] == 7
    assert [step["name"] for step in status["steps"]] == ["data_verify", "example"]
    assert (run / "analysis_bundle.tar.gz").exists()
    assert not (run / "reports/old_report.csv").exists()
    assert (root / "reports/old_report.csv").read_text() == "old result"
    assert (root / "data/frozen.bin").read_bytes() == b"immutable"
    manifest = json.loads((run / "manifest.json").read_text())
    assert "ml_dataset.csv" in manifest["seed_reports"]
    assert manifest["source_tree_sha256"]


def test_training_recipes_and_existing_models(modules):
    _, runner = modules
    for mode in ("ppo-profile", "ppo-train", "ppo-resume"):
        for _, command in runner.recipe(mode, sys.executable, "2026-11-05"):
            assert (runner.ROOT / command[1]).is_file()
    assert "risk" not in runner.MODES  # 不转发 Makefile 尚未实现的 risk stage
    jobs = runner.recipe("models", sys.executable, "2026-11-05")
    assert [name for name, _ in jobs] == ["ml_baseline", "cost_2lot", "cost_1lot", "protocol_full"]


def test_run_directory_cannot_be_inside_source_data(modules, tmp_path):
    _, runner = modules
    with pytest.raises(SystemExit) as error:
        runner.main(["smoke", "--data-dir", str(tmp_path), "--runs-dir", str(tmp_path / "runs")])
    assert error.value.code == 2


def test_ipv4_launcher_preserves_target_arguments(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import socket, sys, urllib3.util.connection\n"
        "assert urllib3.util.connection.allowed_gai_family() == socket.AF_INET\n"
        "assert sys.argv[1:] == ['--end', '2026-11-05']\n"
        "print('probe passed')\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "ipv4_exec.py"), str(probe), "--end", "2026-11-05"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "probe passed" in result.stdout
