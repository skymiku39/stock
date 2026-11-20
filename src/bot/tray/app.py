"""系統匣程式：圖示顏色顯示整體狀態，左鍵開狀態視窗，右鍵選單開報告與儀表板。

    python -m bot.tray            # 或 stock-tray；要在登入的桌面執行
    pythonw -m bot.tray           # 不帶主控台視窗（登入時自動啟動用這個）

這是前台：只讀背景工作寫好的狀態檔（見 bot/tray/status.py），不登入券商、不下單。
排程器照舊在背景執行，關掉這個程式不影響資料收集。

執行緒：Tk 的主迴圈在主執行緒；pystray 用 run_detached 跑在自己的執行緒，選單動作一律
丟進佇列，由 Tk 這邊定時取出執行——Tk 的元件只能在建立它的執行緒操作。
"""
from __future__ import annotations

import argparse
import os
import queue
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

from bot.tray import status as tray_status
from bot.utils import get_logger, now_tw

APP_NAME = "Stock Lab"
REFRESH_SECONDS = 15
DASHBOARD_PORT = 8501
DASHBOARD_URL = f"http://127.0.0.1:{DASHBOARD_PORT}"
DASHBOARD_START_TIMEOUT = 45.0
MUTEX_NAME = "Local\\SkyMikuStockLabTray"
LEVEL_COLOR = {tray_status.OK: "#2e9e5b", tray_status.WARN: "#e0a100",
               tray_status.FAIL: "#d64545", tray_status.UNKNOWN: "#8a8f98"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EDGE_PATHS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")


def make_icon(level: str, size: int = 64):
    """純色圓形加白色 S；顏色代表整體狀態。"""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 3, size - 3), fill=LEVEL_COLOR.get(level, LEVEL_COLOR["unknown"]))
    try:
        font = ImageFont.truetype("segoeuib.ttf", int(size * 0.62))
    except OSError:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), "S", font=font)
    draw.text(((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
              "S", font=font, fill="white")
    return image


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def acquire_single_instance() -> Optional[object]:
    """同一個桌面只跑一份；已有一份在執行時回傳 None。"""
    if sys.platform != "win32":
        return object()
    import ctypes
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle or ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


class Dashboard:
    """現有的網頁儀表板：沒在執行就啟動，再用 Edge 的應用程式模式開獨立視窗。"""

    def __init__(self, root: Path, logger):
        self.root = Path(root)
        self.logger = logger
        self.process: Optional[subprocess.Popen] = None

    def running(self) -> bool:
        return port_open(DASHBOARD_PORT)

    def start(self) -> None:
        if self.running():
            return
        python = Path(sys.executable).with_name("python.exe")  # pythonw 沒有標準輸出，streamlit 會出錯
        script = Path(__file__).resolve().parents[1] / "dashboard" / "nav.py"
        log_dir = self.root / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "tray_dashboard.log").open("ab")
        self.process = subprocess.Popen(
            [str(python if python.exists() else sys.executable), "-m", "streamlit", "run",
             str(script), "--server.address", "127.0.0.1", "--server.port", str(DASHBOARD_PORT),
             "--server.headless", "true"],
            cwd=str(self.root), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=dict(os.environ, STOCK_DASHBOARD_ROOT=str(self.root)), creationflags=_NO_WINDOW)
        self.logger.info("啟動儀表板 pid=%s", self.process.pid)

    def wait_ready(self, timeout: float = DASHBOARD_START_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.running():
                return True
            if self.process is not None and self.process.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def open_window(self) -> None:
        edge = next((p for p in EDGE_PATHS if Path(p).exists()), None)
        if edge:
            subprocess.Popen([edge, f"--app={DASHBOARD_URL}", "--window-size=1500,950"],
                             creationflags=_NO_WINDOW)
        else:
            webbrowser.open(DASHBOARD_URL)

    def stop_if_started_here(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.logger.info("結束由系統匣啟動的儀表板 pid=%s", self.process.pid)
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                           capture_output=True, creationflags=_NO_WINDOW)


class TrayApp:
    def __init__(self, root: Path):
        import tkinter as tk
        self.root = Path(root)
        self.logger = get_logger("tray")
        self.dashboard = Dashboard(self.root, self.logger)
        self.snapshot = tray_status.read_snapshot(self.root, now_tw())
        self.actions: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.window = None
        self.icon = None
        self._icon_level = None
        self._busy = ""  # 進行中的工作，顯示在選單與視窗

    # ---- 開啟類動作 --------------------------------------------------------
    def open_path(self, relative: str) -> None:
        path = self.root / relative
        if not path.exists():
            self.notify(f"找不到 {relative}", "這份檔案還沒有產生。")
            return
        os.startfile(str(path))  # noqa: S606 - 開啟專案自己的報告或資料夾

    def open_dashboard(self) -> None:
        import threading

        def work():
            try:
                if not self.dashboard.running():
                    self._busy = "正在啟動儀表板…"
                    self.dashboard.start()
                    if not self.dashboard.wait_ready():
                        self.actions.put(lambda: self.notify(
                            "儀表板沒有啟動成功", "請看 log/tray_dashboard.log。"))
                        return
                self.dashboard.open_window()
            except Exception:  # noqa: BLE001
                self.logger.exception("開啟儀表板失敗")
                self.actions.put(lambda: self.notify("開啟儀表板失敗", "請看 log 的 tray 紀錄。"))
            finally:
                self._busy = ""

        threading.Thread(target=work, daemon=True).start()

    def open_dashboard_in_browser(self) -> None:
        webbrowser.open(DASHBOARD_URL)

    def notify(self, title: str, body: str) -> None:
        try:
            from bot.local_notify import show_toast
            show_toast(title, body, root=self.root)
        except Exception:  # noqa: BLE001
            self.logger.exception("通知失敗：%s", title)

    # ---- 選單 --------------------------------------------------------------
    def _post(self, fn: Callable[[], None]) -> Callable:
        """選單動作在 pystray 的執行緒觸發，轉交給 Tk 的執行緒執行。"""
        return lambda *_: self.actions.put(fn)

    def build_menu(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        def items():
            snap = self.snapshot
            rows = [Item(f"{tray_status.LEVEL_LABEL[snap.level]}：{snap.headline}"[:80],
                         self._post(self.show_window), default=True)]
            if self._busy:
                rows.append(Item(self._busy, None, enabled=False))
            rows += [Item(h.line()[:80], None, enabled=False) for h in snap.holdings]
            rows += [
                Menu.SEPARATOR,
                Item("狀態視窗", self._post(self.show_window)),
                Item("儀表板（獨立視窗）", self._post(self.open_dashboard)),
                Item("開啟", Menu(
                    Item("ETF 關注名單", self._post(lambda: self.open_path("data/etf_focus/latest.md"))),
                    Item("前瞻追蹤", self._post(lambda: self.open_path("data/forward_tracking/latest.md"))),
                    Item("今日摘要", self._post(lambda: self.open_path(
                        f"data/daily_research/{now_tw().date().isoformat()}/summary.txt"))),
                    Item("通知紀錄", self._post(lambda: self.open_path("data/notifications/history.jsonl"))),
                    Item("盯盤提醒紀錄", self._post(lambda: self.open_path("data/smile_watch/alerts.jsonl"))),
                    Menu.SEPARATOR,
                    Item("log 資料夾", self._post(lambda: self.open_path("log"))),
                    Item(f"儀表板連結 {DASHBOARD_URL}（用預設瀏覽器）",
                         self._post(self.open_dashboard_in_browser)),
                )),
                Menu.SEPARATOR,
                Item("重新整理", self._post(self.refresh)),
                Item("結束系統匣（背景照常執行）", self._post(self.quit)),
            ]
            return rows

        return pystray.Menu(items)

    # ---- 狀態視窗 ----------------------------------------------------------
    def show_window(self) -> None:
        from bot.tray.window import StatusWindow
        if self.window is None:
            self.window = StatusWindow(self)
        self.refresh()
        self.window.show()

    # ---- 更新 --------------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.snapshot = tray_status.read_snapshot(self.root, now_tw())
        except Exception:  # noqa: BLE001
            self.logger.exception("讀取狀態失敗")
            return
        if self.icon is not None:
            if self.snapshot.level != self._icon_level:
                self._icon_level = self.snapshot.level
                self.icon.icon = make_icon(self.snapshot.level)
            self.icon.title = self.snapshot.tooltip()
            self.icon.update_menu()
        if self.window is not None and self.window.visible:
            self.window.render(self.snapshot)

    def _tick(self) -> None:
        self.refresh()
        self.tk.after(REFRESH_SECONDS * 1000, self._tick)

    def _drain(self) -> None:
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except Exception:  # noqa: BLE001
                self.logger.exception("選單動作失敗")
        self.tk.after(100, self._drain)

    def quit(self) -> None:
        self.logger.info("結束系統匣")
        try:
            if self.icon is not None:
                self.icon.stop()
        finally:
            self.dashboard.stop_if_started_here()
            self.tk.quit()

    def run(self) -> int:
        import pystray
        self._icon_level = self.snapshot.level
        self.icon = pystray.Icon("stock-lab", make_icon(self.snapshot.level),
                                 self.snapshot.tooltip(), self.build_menu())
        self.icon.run_detached()
        self.logger.info("系統匣啟動（root=%s）", self.root)
        self.tk.after(100, self._drain)
        self.tk.after(REFRESH_SECONDS * 1000, self._tick)
        try:
            self.tk.mainloop()
        finally:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stock Lab 系統匣（前台顯示與管理）")
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[3], help="專案根目錄")
    parser.add_argument("--show", action="store_true", help="啟動後直接打開狀態視窗")
    args = parser.parse_args(argv)
    if acquire_single_instance() is None:
        print("Stock Lab 系統匣已經在執行。")
        return 0
    app = TrayApp(args.root.resolve())
    if args.show:
        app.actions.put(app.show_window)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
