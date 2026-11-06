import pytest
from shioaji.constant import Action, OrderType, StockOrderLot, StockPriceType
from bot.config import Settings
from bot.stock_db import IntradayBar
from bot.tick_replay import TickPaperApi


def bar(ts, **kwargs):
    return IntradayBar(symbol='0050',ts=ts,interval='tick',close=100,volume=2,
        bid_price=99.9,bid_volume=1,ask_price=100,ask_volume=1,**kwargs)


def test_quotes_are_causal_and_limit_liquidity_and_share_budget():
    api=TickPaperApi(Settings(_env_file=None,max_fund=500000,daily_fund_budget=0),lambda *args:None)
    api.advance(bar('2026-11-05 09:01:00.000000000'))
    first=api.place_order(api.contract('0050'),api.Order(action=Action.Buy,price=100,quantity=2,price_type=StockPriceType.LMT,order_type=OrderType.ROD))
    second=api.place_order(api.contract('0050'),api.Order(action=Action.Buy,price=100,quantity=1,price_type=StockPriceType.LMT,order_type=OrderType.ROD))
    api.advance(bar('2026-11-05 09:01:00.000000000',sequence=1))
    assert not api.messages
    api.advance(bar('2026-11-05 09:01:00.000001000'))
    assert first.status.deal_quantity==1 and second.status.deal_quantity==0
    assert api.fill_evidence[0]['submitted_at']<api.fill_evidence[0]['ts']
    api.advance(bar('2026-11-05 14:30:00.000000000'))
    assert len(api.messages)==1


def test_cannot_use_round_lot_quotes_to_validate_odd_lot_fills():
    api=TickPaperApi(Settings(_env_file=None),lambda *args:None)
    api.advance(bar('2026-11-05 09:01:00.000000000'))
    with pytest.raises(ValueError,match='odd-lot'):
        api.place_order(api.contract('0050'),api.Order(action=Action.Buy,price=100,quantity=1,order_lot=StockOrderLot.IntradayOdd,price_type=StockPriceType.LMT,order_type=OrderType.ROD))


def test_ioc_unmatched_remainder_is_cancelled():
    api=TickPaperApi(Settings(_env_file=None,max_fund=500000,daily_fund_budget=0),lambda *args:None)
    api.advance(bar('2026-11-05 09:01:00.000000000'))
    order=api.place_order(api.contract('0050'),api.Order(action=Action.Buy,price=100,quantity=2,price_type=StockPriceType.LMT,order_type=OrderType.IOC))
    api.advance(bar('2026-11-05 09:01:00.000001000'))
    assert order.status.deal_quantity==1
    assert order.status.status.value=='Cancelled'
