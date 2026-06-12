"""Streamlit AppTest smoke tests for K-line board rendering."""
from __future__ import annotations

from streamlit.testing.v1 import AppTest

from bot.dashboard.common import DASHBOARD_UI_BUILD


def test_kline_board_page_loads_without_exception():
  """K 線看板應可載入且顯示 UI build 標記（原生圖表路徑）。"""
  at = AppTest.from_file("src/bot/dashboard/nav.py")
  at.run(timeout=60)
  assert not at.exception, [str(e) for e in at.exception]

  at.session_state["page"] = "K 線看板"
  at.run(timeout=120)
  assert not at.exception, [str(e) for e in at.exception]

  build_caps = [
      c.value for c in at.caption
      if c.value and DASHBOARD_UI_BUILD in c.value
  ]
  assert build_caps, f"sidebar 應顯示 UI build: {DASHBOARD_UI_BUILD}"

  titles = [t.value for t in at.title if t.value]
  assert any("K" in t and "看板" in t for t in titles), titles
