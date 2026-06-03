#!/usr/bin/env python
"""只讀資料源 smoke test 的便捷入口。

等同於 ``uv run stock-validate``；此檔案讓不想用 console script 的人可直接：

    uv run python scripts/validate_data_sources.py --json out.json --md out.md

實際邏輯都在 ``bot.validate_sources``，方便單元測試 import。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允許直接以 `python scripts/validate_data_sources.py` 執行 (把 src/ 加進路徑)
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bot.validate_sources import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
