#!/usr/bin/env python3
"""Streamlit 仪表盘守护进程。

设计目的：本地 Streamlit 是普通进程，会被系统清理 / 崩溃后无人拉起。
本脚本检测 8501 端口，未监听则重新拉起服务，供 Windows 计划任务每 N 分钟调用。

用法：
    python watchdog.py          # 检测并在需要时拉起
    python watchdog.py --check  # 只检测不拉起，退出码 0=存活 1=未存活
"""

import socket
import subprocess
import sys
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8501

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = Path.home() / ".drzfitness" / ".venv" / "Scripts"
# 优先 pythonw.exe（无控制台窗口的 GUI 版解释器）。
# 用 python.exe 时即使 DETACHED_PROCESS，Windows 仍会为控制台程序新建窗口 → 用户看到 cmd 闪现。
VENV_PYW = VENV_DIR / "pythonw.exe"
VENV_PY = VENV_PY_FALLBACK = VENV_DIR / "python.exe"
LOG_FILE = PROJECT_DIR / "watchdog.log"

# Windows 进程脱离标志：父进程退出后子进程继续存活
DETACHED = 0x00000008  # DETACHED_PROCESS
NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW —— 关键：阻止创建控制台窗口


def is_alive() -> bool:
    """端口可连即视为存活。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex((HOST, PORT)) == 0


def _pick_python():
    """优先选无控制台窗口的解释器；都没有则返回 None。"""
    for cand in (VENV_PYW, VENV_PY):
        if cand.exists():
            return cand
    return None


def launch() -> bool:
    py = _pick_python()
    if py is None:
        _log(f"虚拟环境不存在：{VENV_PY}")
        return False

    # NO_WINDOW 是防弹窗的关键；即便选中 pythonw 也一并带上，避免 venv 结构变化后复发
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = DETACHED | NEW_GROUP | NO_WINDOW

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"\n[{_now()}] watchdog: 服务未存活，尝试拉起（{py.name}）\n")
            subprocess.Popen(
                [
                    str(py),
                    "-m",
                    "streamlit",
                    "run",
                    "app.py",
                    "--server.port",
                    str(PORT),
                    "--server.headless",
                    "true",
                ],
                cwd=str(PROJECT_DIR),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **kwargs,
            )
        _log("已发起拉起")
        return True
    except Exception as exc:  # 守护进程自身不能崩
        _log(f"拉起失败：{exc!r}")
        return False


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"[{_now()}] {msg}\n")
    except Exception:
        pass


def main() -> int:
    if "--check" in sys.argv:
        alive = is_alive()
        print("ALIVE" if alive else "DOWN")
        return 0 if alive else 1

    if is_alive():
        return 0

    return 0 if launch() else 1


if __name__ == "__main__":
    sys.exit(main())
