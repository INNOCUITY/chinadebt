"""仅为当前抓取进程选择 IPv4；不修改系统网络或金融计算。"""

import runpy
import socket
import sys

import urllib3.util.connection


def main() -> None:
    """通过 urllib3 的连接族选择器运行指定脚本，保留脚本参数。"""
    if len(sys.argv) < 2:
        raise SystemExit("usage: ipv4_exec.py SCRIPT [ARGS...]")
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
    sys.argv = sys.argv[1:]
    print("TRANSPORT: urllib3 IPv4 only (process-local)", flush=True)
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
