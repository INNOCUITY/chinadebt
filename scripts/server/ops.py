"""服务器数据包、结果包和完整文件散列工具（传输部分只依赖标准库）。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root: Path) -> dict[str, str]:
    """对全部普通文件取散列；拒绝符号链接，避免打包树外内容。"""
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"目录不能含符号链接：{path}")
        if path.is_file() and path.name not in {".DS_Store", ".gitkeep"}:
            files[path.relative_to(root).as_posix()] = sha256(path)
    return files


def tree_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def checksum_sidecar(archive: Path) -> None:
    archive.with_name(archive.name + ".sha256").write_text(
        f"{sha256(archive)}  {archive.name}\n", encoding="utf-8"
    )


def pack_data(data: Path, output: Path) -> None:
    """打包整个 data 树（包括 current 指向的 versions），不改变源数据。"""
    data, output = data.resolve(), output.resolve()
    if not data.is_dir():
        raise ValueError(f"找不到数据目录：{data}")
    if output.is_relative_to(data) or output.exists():
        raise ValueError("数据包必须写到 data 外的新文件，不能覆盖已有包")
    files = inventory(data)
    if not files:
        raise ValueError("数据目录为空")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "DATA_MANIFEST.json"
            write_json(manifest, {"schema": 1, "files": files, "tree_sha256": tree_digest(files)})
            with tarfile.open(temporary, "w:gz") as archive:
                archive.add(manifest, arcname="DATA_MANIFEST.json")
                for relative in files:
                    archive.add(data / relative, arcname="data/" + relative, recursive=False)
        # 打包中源文件变化时不发布一个自相矛盾的快照。
        if inventory(data) != files:
            raise ValueError("打包期间数据发生变化，请停止写入后重试")
        temporary.replace(output)
        checksum_sidecar(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"DATA_ARCHIVE={output}", flush=True)


def unpack_data(archive_path: Path, destination: Path) -> None:
    """只解包本工具的数据格式，逐文件核验后发布到全新目录。"""
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f"目标已存在，拒绝覆盖：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        stage = Path(tmp) / "snapshot"
        stage.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            seen = set()
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or member.name in seen
                    or not member.isfile()
                    or not (
                        member.name == "DATA_MANIFEST.json"
                        or (path.parts and path.parts[0] == "data" and len(path.parts) > 1)
                    )
                ):
                    raise ValueError(f"数据包包含非法路径/类型：{member.name}")
                seen.add(member.name)
                target = stage / path
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                assert stream is not None
                with stream, target.open("wb") as out:
                    shutil.copyfileobj(stream, out)
        verify_data_bundle(stage)
        stage.rename(destination)
    print(f"FICC_DATA_DIR={destination / 'data'}", flush=True)


def verify_data_bundle(snapshot: Path) -> None:
    manifest = json.loads((snapshot / "DATA_MANIFEST.json").read_text())
    actual = inventory(snapshot / "data")
    if actual != manifest["files"] or tree_digest(actual) != manifest["tree_sha256"]:
        raise ValueError("数据文件 SHA-256/文件集合与 DATA_MANIFEST 不一致")
    print(f"数据包校验通过：{len(actual)} files / {tree_digest(actual)}", flush=True)


def pack_results(run: Path) -> Path:
    """打包审查所需日志/报告/指纹；不带 data、.git、venv、原始源码。"""
    paths = []
    for name in (
        "status.json",
        "manifest.json",
        "environment.json",
        "requirements.freeze.txt",
        "logs",
        "reports",
    ):
        path = run / name
        if path.is_dir():
            paths.extend(p for p in path.rglob("*") if p.is_file() and not p.is_symlink())
        elif path.is_file():
            paths.append(path)
    files = {p.relative_to(run).as_posix(): sha256(p) for p in sorted(paths)}
    manifest_path = run / "BUNDLE_MANIFEST.json"
    write_json(manifest_path, {"schema": 1, "files": files, "tree_sha256": tree_digest(files)})
    output = run / "analysis_bundle.tar.gz"
    temporary = output.with_name(output.name + ".tmp")
    with tarfile.open(temporary, "w:gz") as archive:
        for path in [*paths, manifest_path]:
            archive.add(path, arcname=path.relative_to(run).as_posix(), recursive=False)
    temporary.replace(output)
    checksum_sidecar(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("output", type=Path)
    pack.add_argument("--data-dir", type=Path, default=ROOT / "data")
    unpack = commands.add_parser("unpack")
    unpack.add_argument("archive", type=Path)
    unpack.add_argument("destination", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("snapshot", type=Path)
    results = commands.add_parser("results")
    results.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    if args.command == "pack":
        pack_data(args.data_dir, args.output)
    elif args.command == "unpack":
        unpack_data(args.archive, args.destination)
    elif args.command == "verify":
        verify_data_bundle(args.snapshot)
    elif args.command == "results":
        status = json.loads((args.run_dir / "status.json").read_text())
        if status["state"] not in {"succeeded", "failed", "interrupted"}:
            raise ValueError("任务尚未结束；不要对运行中的结果打最终包")
        print(pack_results(args.run_dir))


if __name__ == "__main__":
    main()
