"""Stock Bot Dashboard package.

The Streamlit application is intentionally imported lazily.  Importing the
package is also how the ``stock-dashboard`` console entry point is resolved;
eagerly importing ``nav`` would initialize Streamlit before ``--help`` can be
handled and would turn a harmless CLI inspection into a long-running server.
"""

from __future__ import annotations

from typing import Any

__all__ = ["main_app", "run"]


def run() -> None:
    """Lazily delegate to the Streamlit console runner."""

    from bot.dashboard.run import run as _run

    return _run()


def __getattr__(name: str) -> Any:
    if name == "main_app":
        from bot.dashboard.nav import main_app

        return main_app
    raise AttributeError(name)
