"""系統匣程式中不需要桌面的部分：圖示、連接埠檢查、儀表板啟動判斷。"""
import socket

from bot.tray import app as tray_app
from bot.tray import status as tray_status


def test_icon_is_square_and_colour_follows_level():
    centres = {}
    for level in (tray_status.OK, tray_status.WARN, tray_status.FAIL, tray_status.UNKNOWN, "other"):
        image = tray_app.make_icon(level)
        assert image.size == (64, 64) and image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0  # 角落透明
        centres[level] = image.getpixel((32, 6))[:3]  # 圓的上緣，不會被字蓋到
    assert len({centres[tray_status.OK], centres[tray_status.WARN], centres[tray_status.FAIL]}) == 3
    assert centres["other"] == centres[tray_status.UNKNOWN]


def test_port_open_follows_connection_result(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(socket, "create_connection", lambda address, timeout=None: Connection())
    assert tray_app.port_open(8501)

    def refuse(address, timeout=None):
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", refuse)
    assert not tray_app.port_open(8501)


def test_dashboard_start_is_noop_when_already_running(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(tray_app, "port_open", lambda port, **kw: True)
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda *a, **k: started.append(a))
    dashboard = tray_app.Dashboard(tmp_path, logger=None)
    dashboard.start()
    assert started == [] and dashboard.process is None
    dashboard.stop_if_started_here()  # 不是這裡啟動的就不去結束它


def test_settings_roundtrip_and_bad_file_falls_back(tmp_path):
    from bot.tray import settings as tray_settings

    assert tray_settings.load(tmp_path) == {"minimize_to_tray": True}
    tray_settings.save(tmp_path, {"minimize_to_tray": False, "unknown": 1})
    assert tray_settings.load(tmp_path) == {"minimize_to_tray": False}
    tray_settings.settings_path(tmp_path).write_text('{"minimize_to_tray": "no"}', encoding="utf-8")
    assert tray_settings.load(tmp_path) == {"minimize_to_tray": True}  # 型別不對用預設
    tray_settings.settings_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert tray_settings.load(tmp_path) == {"minimize_to_tray": True}


def test_content_signature_ignores_clock_and_heartbeat():
    from bot.tray import window as tray_window

    snap = tray_status.TraySnapshot(level=tray_status.OK, headline="一切正常", read_at="a",
                                    heartbeat_age_seconds=3.0)
    later = tray_status.TraySnapshot(level=tray_status.OK, headline="一切正常", read_at="b",
                                     heartbeat_age_seconds=18.0)
    changed = tray_status.TraySnapshot(level=tray_status.WARN, headline="有警告", read_at="b")
    assert tray_window.content_signature(snap) == tray_window.content_signature(later)
    assert tray_window.content_signature(snap) != tray_window.content_signature(changed)
    assert tray_window.content_signature(snap) != tray_window.content_signature(snap, "啟動中")
    assert "18 秒前有心跳" in tray_window.scheduler_line(later)
