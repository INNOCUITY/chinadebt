"""单机服务器任务编排：冻结工作副本、真实退出码、日志和可下载结果包。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ops import inventory, pack_results, tree_digest, write_json

ROOT = Path(__file__).resolve().parents[2]
MODES = (
    "doctor",
    "data-check",
    "offline",
    "fetch",
    "smoke",
    "check",
    "models",
    "acceptance-buyhold",
    "acceptance-rolling",
    "acceptance-extended",
    "research",
    "ppo-profile",
    "ppo-train",
    "ppo-resume",
)
COPY_DIRS = ("src", "scripts", "config", "tests")
COPY_FILES = ("README.md", "Makefile", "pyproject.toml", ".gitignore")


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def capture(command: list[str], cwd: Path | None = None) -> dict:
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=30, check=False
        )
        return {
            "command": command,
            "exit_code": result.returncode,
            "output": result.stdout + result.stderr,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "unavailable": str(exc)}


def environment() -> dict:
    return {
        "observed_at": now(),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "python": sys.version,
        "executable": sys.executable,
        "cpu_count_logical": os.cpu_count(),
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "selected_environment": {
            key: os.environ.get(key)
            for key in (
                "CUDA_VISIBLE_DEVICES",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "SLURM_JOB_ID",
            )
        },
        "commands": [
            capture(cmd)
            for cmd in (
                ["lscpu"],
                ["free", "-h"],
                ["df", "-h", str(ROOT)],
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,uuid,memory.total,driver_version",
                    "--format=csv",
                ],
                ["nvidia-smi", "topo", "-m"],
                [sys.executable, "-m", "pip", "check"],
            )
        ],
    }


def source_inventory(work: Path) -> dict[str, str]:
    files = {}
    for name in COPY_DIRS:
        files.update({f"{name}/{k}": v for k, v in inventory(work / name).items()})
    from ops import sha256

    for name in COPY_FILES:
        if (work / name).is_file():
            files[name] = sha256(work / name)
    return files


def prepare_work(root: Path, work: Path, data: Path, commit: str, allow_dirty: bool) -> dict:
    """每 run 单独 clone；不创建共享写入的 reports 或共享可变数据链接。"""
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(root), str(work)], check=True)
    subprocess.run(["git", "checkout", "--quiet", "--detach", commit], cwd=work, check=True)
    if allow_dirty:
        # 开发自测显式允许；完整文件散列披露实际运行源码，默认仍要求 clean。
        for name in COPY_DIRS:
            shutil.rmtree(work / name, ignore_errors=True)
            shutil.copytree(
                root / name,
                work / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
            )
        for name in COPY_FILES:
            if (root / name).exists():
                shutil.copy2(root / name, work / name)
    # 测试成本脚本依赖冻结预测输入；其余旧报告不冒充此次生成结果。
    seeds = {}
    for name in ("ml_dataset.csv", "ml_oos_predictions.csv"):
        path = work / "reports" / name
        if path.exists():
            seeds[name] = path.read_bytes()
    shutil.rmtree(work / "reports", ignore_errors=True)
    (work / "reports").mkdir()
    for name, content in seeds.items():
        (work / "reports" / name).write_bytes(content)
    shutil.rmtree(work / "data", ignore_errors=True)
    if data.is_dir():
        before = inventory(data)
        shutil.copytree(data, work / "data")
        if inventory(work / "data") != before or inventory(data) != before:
            raise ValueError("复制期间数据发生变化；停止数据写入后重试")
    else:
        (work / "data").mkdir()
    return inventory(work / "reports")


def recipe(mode: str, python: str, data_end: str) -> list[tuple[str, list[str]]]:
    def py(name: str, *args: str) -> list[str]:
        return [python, f"scripts/{name}.py", *args]

    jobs = {
        "doctor": [],
        "ppo-profile": [("cpu_fixed_profile", py("profile_training"))],
        "ppo-train": [
            (
                "cuda_fixed_profile",
                py(
                    "train_rl", "profile", "--device", "cuda", "--output", "reports/ppo-gpu-profile"
                ),
            ),
            (
                "ppo_train",
                py(
                    "train_rl",
                    "train",
                    "--device",
                    "cuda",
                    "--output",
                    "reports/ppo",
                    "--verify-resume",
                ),
            ),
        ],
        "ppo-resume": [
            (
                "ppo_resume",
                py(
                    "train_rl",
                    "resume",
                    "--device",
                    "cuda",
                    "--output",
                    "reports/ppo",
                    "--verify-resume",
                ),
            )
        ],
        "data-check": [],
        "offline": [("offline", py("fetch_data", "--offline", "--end", data_end))],
        "fetch": [("fetch", py("fetch_data", "--end", data_end))],
        "smoke": [
            ("example", py("run_local_example", "--out-dir", "reports/local_example")),
            ("cross_entry", py("run_cross_entry_check", "--out-dir", "reports/cross_entry")),
            (
                "protocol_quick",
                py("run_protocol_smoke", "--quick", "--out-dir", "reports/protocol_quick"),
            ),
        ],
        "check": [
            ("lint", [python, "-m", "ruff", "check", "src", "tests", "scripts"]),
            ("format", [python, "-m", "ruff", "format", "--check", "src", "tests", "scripts"]),
            ("pytest", [python, "-m", "pytest", "tests/", "--junitxml=reports/pytest.xml"]),
        ],
        "models": [
            ("ml_baseline", py("run_ml_baseline")),
            ("cost_2lot", py("run_model_cost_eval", "--band-lots", "2")),
            ("cost_1lot", py("run_model_cost_eval", "--band-lots", "1")),
            ("protocol_full", py("run_protocol_smoke", "--out-dir", "reports/protocol_full")),
        ],
        "acceptance-buyhold": [("acceptance", py("run_acceptance"))],
        "acceptance-rolling": [("acceptance_rolling", py("run_acceptance", "--rolling"))],
        "acceptance-extended": [
            ("acceptance_extended", py("run_acceptance", "--rolling", "--end", "2025-12-31"))
        ],
        "research": [
            ("pipeline", py("run_pipeline", "--all")),
            ("diagnostics", py("fit_diagnostics")),
            ("strategies", py("run_strategies")),
            ("recon", py("run_ledger_recon")),
            ("attribution", py("run_attribution")),
            ("long_sample", py("run_long_sample")),
            ("windows", py("run_windows", "--benchmark-version", "rolling")),
            ("risk", py("run_risk")),
            ("cost_study_quick", py("run_cost_study", "--quick")),
            ("lot_exposure", py("run_lot_exposure")),
        ],
    }
    return jobs[mode]


def execute(command: list[str], work: Path, log: Path, env: dict[str, str]) -> int:
    with log.open("w", encoding="utf-8") as stream:
        stream.write("COMMAND " + repr(command) + "\n")
        stream.flush()
        process = subprocess.Popen(
            command,
            cwd=work,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        try:
            return process.wait()
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument(
        "--ipv4-only", action="store_true", help="仅 fetch 的 urllib3 请求使用 IPv4"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("FICC_DATA_DIR", str(ROOT / "data")))
    )
    parser.add_argument(
        "--runs-dir", type=Path, default=Path(os.environ.get("FICC_RUNS_DIR", str(ROOT / "runs")))
    )
    parser.add_argument(
        "--data-end", default="2026-11-05", help="fetch/offline 的固定截点；不自动刷新为今天"
    )
    parser.add_argument(
        "--allow-dirty", action="store_true", help="仅开发验收；将实际工作源码覆盖到副本并散列"
    )
    parser.add_argument("--checkpoint", type=Path, help="上次更新边界的可信 PPO checkpoint")
    args = parser.parse_args(argv)
    if (args.mode == "ppo-resume") != bool(args.checkpoint):
        parser.error("仅 ppo-resume 需要 --checkpoint")
    if args.checkpoint and not args.checkpoint.is_file():
        parser.error("checkpoint 文件不存在")
    if args.ipv4_only and args.mode != "fetch":
        parser.error("--ipv4-only 仅用于 fetch")
    if args.runs_dir.resolve().is_relative_to(args.data_dir.resolve()):
        parser.error("runs-dir 不能放在 data-dir 内，防止数据复制递归包含运行副本")
    if sys.version_info < (3, 11):  # noqa: UP036 - 启动入口需给未安装项目的旧 Python 明确错误
        parser.error("需要 Python >=3.11")
    repo = capture(["git", "status", "--porcelain"], ROOT)
    if repo.get("exit_code") != 0:
        parser.error("必须从 Git 仓库运行")
    dirty = bool(repo["output"].strip())
    if dirty and not args.allow_dirty and args.mode != "doctor":
        parser.error("工作区不干净：请提交/另建干净 checkout；开发自测可显式 --allow-dirty")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    run_id = (
        datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
        + f"-{args.mode}-{uuid.uuid4().hex[:8]}"
    )
    run = args.runs_dir.resolve() / run_id
    run.mkdir(parents=True, exist_ok=False)
    (run / "logs").mkdir()
    print(f"RUN_DIR={run}\nRUNNER_PID={os.getpid()}", flush=True)
    status = {
        "run_id": run_id,
        "state": "running",
        "started_at": now(),
        "runner_pid": os.getpid(),
        "steps": [],
    }
    write_json(run / "status.json", status)
    manifest = {
        "schema": 1,
        "source_commit": commit,
        "source_repo_state": "dirty" if dirty else "clean",
        "mode": args.mode,
        "transport_ipv4_only": args.ipv4_only,
        "argv": sys.argv,
        "data_source": str(args.data_dir.resolve()),
    }
    write_json(run / "manifest.json", manifest)
    work = run / "work"
    code = 1
    try:
        write_json(run / "environment.json", environment())
        frozen = subprocess.check_output(
            [sys.executable, "-m", "pip", "freeze", "--exclude-editable"], text=True
        )
        (run / "requirements.freeze.txt").write_text(frozen)
        if args.mode != "doctor":
            seeds = prepare_work(ROOT, work, args.data_dir.resolve(), commit, args.allow_dirty)
            sources = source_inventory(work)
            data_files = inventory(work / "data")
            manifest.update(
                {
                    "source_files": sources,
                    "source_tree_sha256": tree_digest(sources),
                    "data_files_before": data_files,
                    "data_tree_sha256_before": tree_digest(data_files),
                    "seed_reports": seeds,
                }
            )
            write_json(run / "manifest.json", manifest)
            jobs = recipe(args.mode, sys.executable, args.data_end)
            if args.mode.startswith("ppo-"):
                baseline = json.loads((work / "config/baselines/server-v2.json").read_text())
                if tree_digest(data_files) != baseline["data_tree_sha256_before"]:
                    raise ValueError("训练数据与已验收服务器快照不匹配")
                if args.checkpoint:
                    jobs[0][1].extend(["--checkpoint", str(args.checkpoint.resolve())])
            if args.ipv4_only:
                jobs = [
                    (name, [command[0], "scripts/server/ipv4_exec.py", *command[1:]])
                    for name, command in jobs
                ]
            verify = ("data_verify", [sys.executable, "scripts/server/verify_data.py"])
            jobs = [*jobs, verify] if args.mode in {"fetch", "offline"} else [verify, *jobs]
            env = dict(os.environ)
            env.update(
                {
                    "PYTHONPATH": str(work / "src"),
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "MPLBACKEND": "Agg",
                    "MPLCONFIGDIR": str(run / "mplconfig"),
                }
            )
            for key in (
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            ):
                env.setdefault(key, "1")
            manifest["effective_runtime_environment"] = {
                key: env.get(key)
                for key in (
                    "PYTHONPATH",
                    "CUDA_VISIBLE_DEVICES",
                    "OMP_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                    "MPLBACKEND",
                )
            }
            write_json(run / "manifest.json", manifest)
            for name, command in jobs:
                step = {"name": name, "command": command, "state": "running", "started_at": now()}
                status["steps"].append(step)
                write_json(run / "status.json", status)
                start = time.monotonic()
                print(f"START {name} -> {run / 'logs' / (name + '.log')}", flush=True)
                returncode = execute(command, work, run / "logs" / (name + ".log"), env)
                step.update(
                    {
                        "exit_code": returncode,
                        "elapsed_seconds": time.monotonic() - start,
                        "state": "succeeded" if returncode == 0 else "failed",
                    }
                )
                write_json(run / "status.json", status)
                if returncode != 0:
                    raise RuntimeError(f"步骤 {name} 失败，exit={returncode}；见对应日志")
            data_after = inventory(work / "data")
            manifest.update(
                {"data_files_after": data_after, "data_tree_sha256_after": tree_digest(data_after)}
            )
            if args.mode not in {"fetch", "offline", "check"} and data_after != data_files:
                raise RuntimeError("只读研究阶段改变了数据；拒绝标记成功")
        status["state"] = "succeeded"
        code = 0
    except KeyboardInterrupt:
        status.update({"state": "interrupted", "error": "收到中断；子进程组已停止"})
        code = 130
    except Exception as exc:
        status.update({"state": "failed", "error": f"{type(exc).__name__}: {exc}"})
        print(status["error"], file=sys.stderr, flush=True)
    finally:
        status.update({"finished_at": now(), "exit_code": code})
        for step in status["steps"]:
            if step["state"] == "running":
                step["state"] = status["state"]
        write_json(run / "manifest.json", manifest)
        write_json(run / "status.json", status)
        if (work / "reports").exists():
            shutil.copytree(work / "reports", run / "reports", dirs_exist_ok=True)
        print(f"STATE={status['state']}\nBUNDLE={pack_results(run)}", flush=True)
        if args.mode in {"fetch", "offline"}:
            print(f"DATA_DIR={work / 'data'}", flush=True)
    return code


if __name__ == "__main__":

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
