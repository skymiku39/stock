"""Stock Bot Dashboard -- Streamlit 入口 shim。

啟動方式 (任一即可):
    uv run stock-dashboard
    uv run streamlit run src/bot/dashboard.py
"""

from bot.dashboard import main_app, run

__all__ = ["main_app", "run"]

if __name__ == "__main__":
    main_app()
