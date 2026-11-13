import pytest

from chiefvoice.agents.post_call.tools import (
    build_post_call_tools,
    calculate,
    format_time,
)


def test_calculator_evaluates_safe_arithmetic():
    assert calculate("(18 + 6) / 4") == {"result": 6.0}


def test_calculator_rejects_code_and_names():
    with pytest.raises(ValueError):
        calculate("__import__('os').system('whoami')")


def test_time_tool_returns_configured_zone_and_explicit_offset():
    result = format_time(
        "2026-01-01T00:00:00Z",
        "Asia/Kolkata",
        "%Y-%m-%dT%H:%M:%S%z",
    )
    assert result == {
        "formatted": "2026-01-01T05:30:00+0530",
        "iso8601": "2026-01-01T05:30:00+05:30",
        "time_zone": "Asia/Kolkata",
    }


def test_time_tool_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        format_time("2026-01-01T00:00:00", "UTC", "%Y-%m-%dT%H:%M:%S%z")


def test_only_selected_tools_are_exposed():
    tools = build_post_call_tools(
        ("time",), time_zone="Asia/Kolkata", time_format="%Y-%m-%dT%H:%M:%S%z"
    )
    assert [tool.name for tool in tools] == ["time"]
