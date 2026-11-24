"""富果盯盤備援：假 client，不連網。"""
import datetime as dt

from bot.free_sources.base import SourceError
from bot.free_sources.fugle import describe_quote, limit_lock
from bot.free_sources.quota import TAIPEI, QuotaExceeded
from bot.smile_watch import build_fugle_client, resolve_price
from types import SimpleNamespace

TODAY = dt.date(2026, 11, 24)
PRIMARY_OK = {"price": 76.0, "quote_date": "2026-11-24", "price_basis": "last_trade"}
PRIMARY_BAD = {"price": 75.0, "quote_date": "2026-11-23", "price_basis": "last_trade"}


def fq(**over):
    t = int(dt.datetime(2026, 11, 24, 11, 1, 16, tzinfo=TAIPEI).timestamp() * 1_000_000)
    q = {"lastTrade": {"price": 76.4, "time": t}}
    q.update(over)
    return q


class FakeFugle:
    def __init__(self, quote=None, exc=None):
        self.quote, self.exc, self.calls = quote, exc, []

    def intraday_quote(self, symbol, oddlot=False):
        self.calls.append(symbol)
        if self.exc:
            raise self.exc
        return self.quote


def test_primary_quote_never_calls_fugle():
    f = FakeFugle(fq())
    assert resolve_price("1514", PRIMARY_OK, TODAY, False, f) == (76.0, "primary", "")
    assert f.calls == []


def test_fallback_used_only_when_primary_missing():
    for primary in (None, PRIMARY_BAD):
        f = FakeFugle(fq())
        price, source, note = resolve_price("1514", primary, TODAY, False, f)
        assert (price, source) == (76.4, "fugle") and f.calls == ["1514"]
        assert "11:01:16" in note


def test_no_key_builds_no_client():
    for key in ("", "   ", None):
        assert build_fugle_client(SimpleNamespace(fugle_api_key=key)) is None
    assert build_fugle_client(SimpleNamespace(fugle_api_key="k")) is not None


def test_no_client_behaves_as_before():
    assert resolve_price("1514", None, TODAY, False, None) is None
    assert resolve_price("1514", PRIMARY_BAD, TODAY, False, None) is None
    assert resolve_price("1514", PRIMARY_OK, TODAY, False, None)[0] == 76.0
    assert resolve_price("1514", PRIMARY_BAD, TODAY, True, None)[0] == 75.0  # --once 沿用舊行為


def test_untrusted_fugle_quotes_are_rejected():
    for bad in (fq(isTrial=True), fq(tradingHalt={"isHalted": True}), fq(lastTrade=None)):
        assert resolve_price("1514", None, TODAY, False, FakeFugle(bad)) is None
    assert resolve_price("1514", None, dt.date(2026, 11, 25), False, FakeFugle(fq())) is None


def test_limit_lock_is_reported_not_dropped():
    up = describe_quote(fq(isLimitUpPrice=True, isLimitUpBid=True), TODAY)
    assert up["lock"] == "limit_up" and up["price"] == 76.4
    assert up["note"] == "鎖漲停、最後成交價 76.4、時間 11:01:16"
    down = describe_quote(fq(isLimitDownPrice=True, isLimitDownAsk=True), TODAY)
    assert down["note"].startswith("鎖跌停、最後成交價 76.4")
    assert limit_lock(fq(isLimitUpPrice=True)) is None  # 只有價在漲停、買價沒鎖
    assert describe_quote(fq(), TODAY)["lock"] is None
    got = resolve_price("1514", None, TODAY, False,
                        FakeFugle(fq(isLimitUpPrice=True, isLimitUpBid=True)))
    assert got[1] == "fugle" and got[2].startswith("鎖漲停")


def test_fugle_errors_do_not_break_the_loop():
    for exc in (SourceError("fugle", 401, "bad"), SourceError("fugle", None, "x", "network"),
                QuotaExceeded("fugle", "day", 3000)):
        assert resolve_price("1514", None, TODAY, False, FakeFugle(exc=exc)) is None
