"""Tests for MOPS scraper link parsing and detail helpers."""
from __future__ import annotations

import logging

import datetime as dt

from bot.mops_scraper import (
    MOPS_CALENDAR_FILE_PREFIX,
    MaterialInfo,
    _extract_url_from_onclick,
    _html_to_plain_text,
    _href_from_tr_html,
    _merge_material_lists,
    _parse_conference_html,
    _parse_material_html,
    is_meaningful_presentation_url,
)


SAMPLE_CONFERENCE_HTML = """
<table>
<tr>
  <td>115/06/05</td>
  <td>10:00</td>
  <td>2330</td>
  <td>台積電</td>
  <td><a href="/mops/web/redirectPath?url=files/test.pdf">線上法說會</a></td>
</tr>
</table>
"""

SAMPLE_MATERIAL_HTML = """
<table>
<tr>
  <td>115/06/01</td>
  <td>15:30</td>
  <td><a href="/mops/web/t05st10?seq=123">本公司召開法人說明會</a></td>
  <td>公告</td>
</tr>
</table>
"""


def test_href_from_tr_html_extracts_relative_url():
    tr = '<tr><td><a href="/mops/web/t05st10?seq=1">detail</a></td></tr>'
    href = _href_from_tr_html(tr)
    assert "t05st10" in href
    assert href.startswith("http")


def test_parse_conference_html_extracts_presentation_url():
    log = logging.getLogger("test")
    entries = _parse_conference_html(SAMPLE_CONFERENCE_HTML, 115, 6, log)
    assert len(entries) == 1
    assert entries[0].ticker == "2330"
    assert entries[0].company == "台積電"
    assert "test.pdf" in entries[0].presentation_url


def test_parse_material_html_extracts_detail_url():
    log = logging.getLogger("test")
    items = _parse_material_html(SAMPLE_MATERIAL_HTML, "2330", log)
    assert len(items) == 1
    assert "法人說明會" in items[0].subject
    assert "t05st10" in items[0].detail_url


SAMPLE_ONCLICK_CONFERENCE_HTML = """
<table>
<tr onclick="openWindow('/mops/web/redirectPath?url=files/onclick.pdf')">
  <td>115/06/06</td>
  <td>14:00</td>
  <td>2317</td>
  <td>鴻海</td>
  <td>線上法說會</td>
</tr>
</table>
"""


def test_extract_url_from_onclick_open_window():
    url = _extract_url_from_onclick(
        "openWindow('/mops/web/redirectPath?url=files/test.pdf')"
    )
    assert "test.pdf" in url
    assert url.startswith("http")


def test_href_from_tr_html_parses_onclick_on_tr():
    tr = (
        '<tr onclick="openWindow(\'/mops/web/t05st10?seq=9\')">'
        "<td>detail</td></tr>"
    )
    href = _href_from_tr_html(tr)
    assert "t05st10" in href


def test_parse_conference_html_onclick_presentation_url():
    log = logging.getLogger("test")
    entries = _parse_conference_html(SAMPLE_ONCLICK_CONFERENCE_HTML, 115, 6, log)
    assert len(entries) == 1
    assert entries[0].ticker == "2317"
    assert "onclick.pdf" in entries[0].presentation_url


def test_merge_material_lists_adds_detail_url_from_mops():
    d = dt.date(2026, 6, 9)
    openapi = [
        MaterialInfo(
            date=d, time="15:00", ticker="2330", company="台積電",
            subject="本公司召開法人說明會", detail_url="",
        ),
    ]
    mops = [
        MaterialInfo(
            date=d, time="15:00", ticker="2330", company="台積電",
            subject="本公司召開法人說明會",
            detail_url="https://mopsov.twse.com.tw/mops/web/t05st10?seq=1",
        ),
    ]
    merged = _merge_material_lists(openapi, mops)
    assert len(merged) == 1
    assert "t05st10" in merged[0].detail_url


SAMPLE_FM_FILE_DOWNLOAD_HTML = """
<table>
<tr class='odd' data-type='body'>
  <td>1104</td><td>環泥</td>
  <td align='center'>115/05/22</td>
  <td align='center'>14:00</td>
  <td>台北</td>
  <td>法人說明會</td>
  <td><a href='#' onclick='document.fm_fileDownload.fileName.value="110420260522M001.pdf";document.fm_fileDownload.submit();'><u>110420260522M001.pdf</u></a></td>
  <td><a href='#' onclick='document.fm_fileDownload.fileName.value="110420260522E001.pdf";document.fm_fileDownload.submit();'><u>110420260522E001.pdf</u></a></td>
</tr>
</table>
"""

SAMPLE_FM_FILE_DOWNLOAD_WITH_WEBCAST_HTML = """
<table>
<tr class='odd' data-type='body'>
  <td>2330</td><td>台積電</td>
  <td align='center'>115/05/29</td>
  <td align='center'>08:30</td>
  <td>台北</td>
  <td>法人說明會</td>
  <td><a href='#' onclick='document.fm_fileDownload.fileName.value="233020260529M001.pdf";document.fm_fileDownload.submit();'>pdf</a></td>
  <td><a href='http://webcast.example.com/live'>直播</a></td>
</tr>
</table>
"""


def test_parse_conference_html_fm_file_download_pdf():
    log = logging.getLogger("test")
    entries = _parse_conference_html(SAMPLE_FM_FILE_DOWNLOAD_HTML, 115, 5, log)
    assert len(entries) == 1
    assert entries[0].ticker == "1104"
    assert entries[0].presentation_url.startswith(MOPS_CALENDAR_FILE_PREFIX)
    assert "110420260522M001.pdf" in entries[0].presentation_url


def test_parse_conference_html_prefers_http_webcast():
    log = logging.getLogger("test")
    entries = _parse_conference_html(
        SAMPLE_FM_FILE_DOWNLOAD_WITH_WEBCAST_HTML, 115, 5, log,
    )
    assert entries[0].presentation_url == "http://webcast.example.com/live"


def test_is_meaningful_presentation_url_rejects_bare_host():
    assert not is_meaningful_presentation_url("https://mopsov.twse.com.tw")
    assert is_meaningful_presentation_url(
        f"{MOPS_CALENDAR_FILE_PREFIX}110420260522M001.pdf"
    )


def test_html_to_plain_text_strips_tags():
    text = _html_to_plain_text(
        "<html><body><table>"
        "<tr><td>重大訊息</td><td>內容</td></tr>"
        "<tr><td>A</td><td>B</td></tr>"
        "</table></body></html>"
    )
    assert "重大訊息" in text
    assert "A" in text and "B" in text
