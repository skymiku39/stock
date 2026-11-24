"""本機通知：排隊、顯示、過期與失敗處理（不跳真的通知、不碰專案 data/）。"""
import datetime as dt
import json
import sys

import pytest

from bot.local_notify import LocalNotifier, NotificationQueue, split_text

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=TZ)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="本機通知只在 Windows 啟用")


def history(queue):
    return [json.loads(line) for line in queue.history_path.read_text(encoding="utf-8").splitlines()]


def test_split_text_uses_first_line_as_title_and_strips_tags():
    assert split_text("⚠️ <b>異常</b>\n第一行\n\n第二行") == ("⚠️ 異常", "第一行\n第二行")
    assert split_text("只有標題") == ("只有標題", "")
    assert split_text("  \n") == ("Stock Lab", "")
    # 不是標籤的角括號要保留（例如「價格 < 停損」）
    assert split_text("價格 < 停損 > 0") == ("價格 < 停損 > 0", "")


def test_flush_shows_in_order_and_empties_queue(tmp_path):
    queue = NotificationQueue(tmp_path)
    queue.add("第一則", "a", NOW)
    queue.add("第二則", "b", NOW + dt.timedelta(seconds=1))
    shown = []
    counts = queue.flush(lambda t, b: shown.append((t, b)), now=NOW + dt.timedelta(minutes=1))
    assert shown == [("第一則", "a"), ("第二則", "b")]
    assert counts == {"shown": 2, "expired": 0, "failed": 0, "invalid": 0}
    assert queue.pending() == []
    assert [row["result"] for row in history(queue)] == ["shown", "shown"]


def test_flush_expires_old_and_drops_invalid(tmp_path):
    queue = NotificationQueue(tmp_path)
    queue.add("昨天的", "", NOW - dt.timedelta(hours=13))
    queue.add("剛剛的", "", NOW)
    (queue.pending_dir / "0-bad.json").write_text("{not json", encoding="utf-8")
    shown = []
    counts = queue.flush(lambda t, b: shown.append(t), now=NOW)
    assert shown == ["剛剛的"]
    assert counts == {"shown": 1, "expired": 1, "failed": 0, "invalid": 1}
    assert queue.pending() == []


def test_flush_keeps_failed_for_next_time(tmp_path):
    queue = NotificationQueue(tmp_path)
    queue.add("會失敗", "", NOW)

    def broken(title, body):
        raise RuntimeError("no desktop")

    assert queue.flush(broken, now=NOW)["failed"] == 1
    assert len(queue.pending()) == 1
    shown = []
    assert queue.flush(lambda t, b: shown.append(t), now=NOW)["shown"] == 1
    assert shown == ["會失敗"] and queue.pending() == []


@windows_only
def test_desktop_session_shows_directly(tmp_path):
    shown, triggered = [], []
    notifier = LocalNotifier(True, base_dir=tmp_path, desktop=True,
                             show=lambda t, b: shown.append((t, b)),
                             trigger=lambda: triggered.append(1))
    notifier.send("微笑盯盤 1514：接近單檔停損\n現價 66.00")
    assert shown == [("微笑盯盤 1514：接近單檔停損", "現價 66.00")]
    assert triggered == [] and notifier.queue.pending() == []
    assert history(notifier.queue)[0]["result"] == "shown"


@windows_only
def test_background_session_queues_and_triggers_task(tmp_path):
    shown, triggered = [], []
    notifier = LocalNotifier(True, base_dir=tmp_path, desktop=False,
                             show=lambda t, b: shown.append(t),
                             trigger=lambda: triggered.append(1))
    notifier.send("標題\n內文")
    assert shown == [] and triggered == [1]
    entry = json.loads(notifier.queue.pending()[0].read_text(encoding="utf-8"))
    assert (entry["title"], entry["body"]) == ("標題", "內文")


@windows_only
def test_send_never_raises_and_falls_back_to_queue(tmp_path):
    def broken_show(title, body):
        raise RuntimeError("toast failed")

    def broken_trigger():
        raise RuntimeError("task not registered")

    notifier = LocalNotifier(True, base_dir=tmp_path, desktop=True,
                             show=broken_show, trigger=broken_trigger)
    notifier.send("第一則")
    notifier.send("第二則")
    assert len(notifier.queue.pending()) == 2


def test_disabled_does_nothing(tmp_path):
    notifier = LocalNotifier(False, base_dir=tmp_path, desktop=True,
                             show=lambda t, b: pytest.fail("不應顯示"),
                             trigger=lambda: pytest.fail("不應觸發"))
    assert not notifier.enabled
    notifier.send("不會出現")
    assert notifier.queue.pending() == []
