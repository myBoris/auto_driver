"""原生 SDK 退出保护：先协作停止，再仅终止本终端创建的子进程树。"""

import os
import subprocess

def _stop_capture_process(process, stop) -> None:
    stop.set()
    process.join(timeout=0.5)
    if process.is_alive():
        # Windows 虚拟环境启动器可能另有解释器子进程，必须一并回收本次采集树。
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=2, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        else:
            process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    process.close()
