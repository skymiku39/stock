"""本機通知：在這台電腦的桌面右下角跳出 Windows 通知（不經任何外部服務）。

    python -m bot.local_notify --test "測試訊息"   # 直接跳一則
    python -m bot.local_notify --flush             # 顯示排隊中的通知（給排程工作用）
    python -m bot.local_notify --status            # 看排隊數量與最近紀錄

為什麼要排隊：排程器是以 S4U 方式登入，程序在工作階段 0，從那裡發的通知不會出現在
使用者的桌面。所以背景程序只把通知寫進 `data/notifications/pending/`，再觸發一個
「以互動方式登入」的排程工作（`scripts/install_local_notify_task.ps1` 註冊）到桌面上顯示。
已經在桌面工作階段的程序（手動執行）則直接顯示。

不新增套件：用 Windows PowerShell 內建的 WinRT 通知介面（`scripts/show_toast.ps1`）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional

from bot.utils import configure_utf8_stdio, get_logger, now_tw

TASK_NAME = "SkyMiku Stock Local Notify"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_AGE_HOURS = 12.0  # 排隊超過這個時間的通知不再跳出，只記為 expired
_TAG = re.compile(r"</?(?:b|i|u|pre|code)>", re.IGNORECASE)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def in_desktop_session() -> bool:
    """這個程序是否在使用者的桌面工作階段（工作階段 0 是服務與 S4U 排程）。"""
    if sys.platform != "win32":
        return False
    import ctypes
    session = ctypes.c_ulong(0)
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return False
    return session.value != 0


def split_text(text: str) -> tuple:
    """第一行當標題，其餘當內文；拿掉 Telegram 用的簡單 HTML 標籤。"""
    lines = [line.strip() for line in _TAG.sub("", str(text)).splitlines() if line.strip()]
    if not lines:
        return "Stock Lab", ""
    return lines[0], "\n".join(lines[1:])


def show_toast(title: str, body: str, *, root: Path = PROJECT_ROOT, timeout: float = 20.0) -> None:
    """直接顯示一則通知；失敗丟 RuntimeError。只能在桌面工作階段使用。"""
    script = Path(root) / "scripts" / "show_toast.ps1"
    env = dict(os.environ, STOCK_TOAST_TITLE=title, STOCK_TOAST_BODY=body)
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            env=env, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"顯示通知失敗：{type(exc).__name__}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"顯示通知失敗（exit {done.returncode}）：{detail[:300]}")


def run_flush_task(timeout: float = 15.0) -> None:
    """觸發桌面上的排程工作來顯示排隊中的通知；失敗丟 RuntimeError。"""
    try:
        done = subprocess.run(["schtasks.exe", "/Run", "/TN", TASK_NAME],
                              capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or b"").decode("mbcs", "replace").strip()
        raise RuntimeError(f"exit {done.returncode}：{detail[:200]}")


class NotificationQueue:
    """一則通知一個檔案，不需要鎖；顯示後移除並記到 history.jsonl。"""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.pending_dir = self.base_dir / "pending"
        self.history_path = self.base_dir / "history.jsonl"

    def add(self, title: str, body: str, now: Optional[dt.datetime] = None) -> Path:
        now = now or now_tw()
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        path = self.pending_dir / f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"ts": now.isoformat(timespec="seconds"), "title": title,
                                    "body": body}, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        return path

    def pending(self) -> list:
        if not self.pending_dir.exists():
            return []
        return sorted(self.pending_dir.glob("*.json"))

    def record(self, entry: dict, result: str, now: Optional[dt.datetime] = None) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        row = dict(entry, result=result,
                   handled_at=(now or now_tw()).isoformat(timespec="seconds"))
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def flush(self, show: Callable[[str, str], None], now: Optional[dt.datetime] = None,
              max_age_hours: float = MAX_AGE_HOURS) -> dict:
        """顯示所有排隊中的通知。顯示失敗的留在佇列，下次再試。"""
        counts = {"shown": 0, "expired": 0, "failed": 0, "invalid": 0}
        failed: set = set()
        while True:
            todo = [p for p in self.pending() if p not in failed]
            if not todo:
                return counts
            for path in todo:
                current = now or now_tw()
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    queued = dt.datetime.fromisoformat(entry["ts"])
                    title, body = str(entry["title"]), str(entry.get("body", ""))
                except FileNotFoundError:
                    continue  # 另一個 flush 已處理
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self.record({"file": path.name, "error": f"{type(exc).__name__}: {exc}"},
                                "invalid", current)
                    path.unlink(missing_ok=True)
                    counts["invalid"] += 1
                    continue
                if (current - queued).total_seconds() > max_age_hours * 3600:
                    self.record(entry, "expired", current)
                    path.unlink(missing_ok=True)
                    counts["expired"] += 1
                    continue
                try:
                    show(title, body)
                except RuntimeError as exc:
                    self.record(dict(entry, error=str(exc)), "failed", current)
                    failed.add(path)
                    counts["failed"] += 1
                    continue
                self.record(entry, "shown", current)
                path.unlink(missing_ok=True)
                counts["shown"] += 1


class LocalNotifier:
    """本機通知通道；介面與 TelegramNotifier 的 `enabled`、`send` 相同。

    `send` 不丟例外：通知失敗只記 log，不能讓盯盤或策略停下來。
    """

    def __init__(self, enabled: bool = True, *, base_dir: Optional[Path] = None, logger=None,
                 desktop: Optional[bool] = None,
                 show: Callable[[str, str], None] = show_toast,
                 trigger: Callable[[], None] = run_flush_task):
        self.logger = logger or get_logger("local-notify")
        self._enabled = bool(enabled) and sys.platform == "win32"
        self._desktop = in_desktop_session() if desktop is None else desktop
        self._show = show
        self._trigger = trigger
        self.queue = NotificationQueue(base_dir or PROJECT_ROOT / "data" / "notifications")
        self._trigger_warned = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return "直接顯示" if self._desktop else f"排隊後由排程工作「{TASK_NAME}」顯示"

    def send(self, text: str) -> None:
        if not self._enabled:
            return
        title, body = split_text(text)
        try:
            if self._desktop:
                try:
                    self._show(title, body)
                    self.queue.record({"ts": now_tw().isoformat(timespec="seconds"),
                                       "title": title, "body": body}, "shown")
                    return
                except RuntimeError as exc:
                    self.logger.warning("%s；改為排隊", exc)
            self.queue.add(title, body)
            try:
                self._trigger()
            except RuntimeError as exc:
                if not self._trigger_warned:
                    self._trigger_warned = True
                    self.logger.warning(
                        "本機通知已排隊，但觸發排程工作「%s」失敗（%s）。"
                        "請先執行 scripts/install_local_notify_task.ps1 註冊；"
                        "排隊中的通知會在下次成功時顯示", TASK_NAME, exc)
        except Exception:  # noqa: BLE001 - 通知不能讓呼叫端失敗
            self.logger.exception("本機通知失敗")


def main(argv=None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="本機通知（Windows 桌面通知）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--flush", action="store_true", help="顯示排隊中的通知")
    group.add_argument("--test", metavar="TEXT", help="直接顯示一則測試通知")
    group.add_argument("--queue-test", metavar="TEXT",
                       help="走排隊路徑：寫入佇列並觸發排程工作（模擬背景程序）")
    group.add_argument("--status", action="store_true", help="列出排隊數量與最近紀錄")
    parser.add_argument("--base-dir", default=str(PROJECT_ROOT / "data" / "notifications"))
    args = parser.parse_args(argv)
    queue = NotificationQueue(Path(args.base_dir))

    if args.flush:
        counts = queue.flush(show_toast)
        print(json.dumps(counts, ensure_ascii=False))
        return 1 if counts["failed"] else 0
    if args.test is not None:
        title, body = split_text(args.test.replace("\\n", "\n"))
        show_toast(title, body)
        print("已送出通知")
        return 0
    if args.queue_test is not None:
        notifier = LocalNotifier(True, base_dir=Path(args.base_dir), desktop=False)
        notifier.send(args.queue_test.replace("\\n", "\n"))
        print(f"已排隊，目前佇列 {len(queue.pending())} 則")
        return 0
    print(f"桌面工作階段：{'是' if in_desktop_session() else '否'}")
    print(f"排隊中：{len(queue.pending())} 則")
    if queue.history_path.exists():
        for line in queue.history_path.read_text(encoding="utf-8").splitlines()[-10:]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
