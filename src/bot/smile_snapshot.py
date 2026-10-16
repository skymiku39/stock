"""Verified frozen inputs shared by CLI and dashboard."""
import hashlib
import json
from bot.stock_db import PriceBar


def load_snapshot(root, baseline):
    report = json.loads((baseline / "comparison.json").read_text(encoding="utf-8"))
    raw = (baseline / "inputs.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != report["input_sha256"]:
        raise ValueError("snapshot hash mismatch")
    rows = json.loads(raw)
    cache = {}
    for symbol, bars in rows.items():
        for bar in bars:
            sid = bar["source_record_id"]
            if sid not in cache:
                doc = report["documents"][sid]
                path = (root / doc["path"]).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("source outside project")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != doc["sha256"]:
                    raise ValueError("raw hash mismatch")
                cache[sid] = json.loads(data)
            data = cache[sid]
            y,m,d = bar["date"].split("-")
            matches = [dict(zip(data["fields"],r)) for r in data["data"] if r[0] == f"{int(y)-1911}/{m}/{d}"]
            if len(matches) != 1 or symbol not in data["title"].split():
                raise ValueError("raw identity mismatch")
            # The previous replay verified OHLC; verify volume now that it affects fills.
            shares = int(matches[0]["成交股數"].replace(",", ""))
            if abs(shares/1000 - bar["volume"]) > 1e-8:
                raise ValueError(f"volume mismatch {symbol} {bar['date']}")
    return report, {s:[PriceBar(**r) for r in bars] for s,bars in rows.items()}
