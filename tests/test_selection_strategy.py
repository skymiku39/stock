import datetime as dt

from bot.selection_strategy import C2_AXES, C2_FIXED, compare_c2, render
from bot.stock_db import PriceBar


def bars(symbol, closes, start=dt.date(2020, 1, 1)):
    opens = [closes[0]] + list(closes[:-1])
    return [PriceBar(symbol, (start + dt.timedelta(days=i)).isoformat(), open=o, high=max(o, c) + 1,
                     low=min(o, c) - 1, close=c, volume=100000)
            for i, (o, c) in enumerate(zip(opens, closes))]


def test_compare_c2_train_only_grid_and_render():
    n = 120
    histories = {"A": bars("A", [100 + i for i in range(n)]), "B": bars("B", [100] * n),
                 "C": bars("C", [200 - i for i in range(n)])}
    revenue = {s: [("2019-12-11", 10.0)] for s in histories}
    data = dict(train=(histories, {}), warm=(histories, {}))
    result = compare_c2(data, revenue, validate=False)
    combos = result["strategies"]["c2"]["combos"]
    assert len(combos) == len(C2_AXES[0][1]) * len(C2_AXES[1][1])
    assert sum(c["chosen"] for c in combos) == 1
    assert all(c["train"]["round_count"] + c["train"]["filled_buys"] > 0 for c in combos)
    assert result["c2_defaults"]["entry"] == C2_FIXED["entry"]
    text = render(result)
    assert "選股策略 C2" in text and "持有期限" in text
