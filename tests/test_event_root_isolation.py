from pathlib import Path

from bot.app_bootstrap import get_or_create_bus
from bot.events import BotStarted, reset_event_bus


def test_event_bus_and_jsonl_are_isolated_by_project_root(tmp_path: Path) -> None:
    reset_event_bus()
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"

    bus_a = get_or_create_bus(project_root=root_a)
    bus_a.publish(BotStarted(symbols=("2330",), run_mode="watch", simulation=True))
    bus_b = get_or_create_bus(project_root=root_b)
    bus_b.publish(BotStarted(symbols=("2330",), run_mode="report", simulation=True))

    assert bus_a is not bus_b
    path_a = root_a / "data" / "events" / "domain_events.jsonl"
    path_b = root_b / "data" / "events" / "domain_events.jsonl"
    assert path_a.exists()
    assert path_b.exists()
    assert '"run_mode": "watch"' in path_a.read_text(encoding="utf-8")
    assert '"run_mode": "report"' in path_b.read_text(encoding="utf-8")
    assert '"run_mode": "report"' not in path_a.read_text(encoding="utf-8")

    reset_event_bus()
