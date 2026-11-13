"""Deterministic, local tools available to post-call extraction."""

from __future__ import annotations

import ast
import json
import math
import operator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field


class CalculatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str = Field(min_length=1, max_length=200)


class TimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: str = Field(
        min_length=1,
        max_length=100,
        description="An ISO 8601 timestamp with an explicit UTC offset, such as 2026-11-14T09:30:00Z.",
    )


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _evaluate(node: ast.AST, depth: int = 0) -> int | float:
    if depth > 20:
        raise ValueError("calculator expression is too complex")
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        if abs(node.value) > 1e100:
            raise ValueError("calculator value is outside the supported range")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate(node.operand, depth + 1))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate(node.left, depth + 1)
        right = _evaluate(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("calculator exponent is outside the supported range")
        result = _BINARY_OPERATORS[type(node.op)](left, right)
        if not isinstance(result, (int, float)) or not math.isfinite(result) or abs(result) > 1e100:
            raise ValueError("calculator result is outside the supported range")
        return result
    raise ValueError("calculator accepts arithmetic expressions only")


def calculate(expression: str) -> dict[str, int | float]:
    """Evaluate bounded arithmetic without names, calls, or Python execution."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("calculator expression is invalid") from exc
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ValueError("calculator expression is too complex")
    value = _evaluate(tree.body)
    return {"result": value}


def format_time(timestamp: str, time_zone: str, time_format: str) -> dict[str, str]:
    """Format an aware timestamp in a configured IANA zone, retaining its offset."""
    if "%z" not in time_format:
        raise ValueError("time format must include %z so the UTC offset is explicit")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include an explicit UTC offset")
        local = parsed.astimezone(ZoneInfo(time_zone))
        formatted = local.strftime(time_format)
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timestamp must be a valid ISO 8601 value and time zone") from exc
    return {
        "formatted": formatted,
        "iso8601": local.isoformat(timespec="seconds"),
        "time_zone": time_zone,
    }


def build_post_call_tools(
    selected: tuple[str, ...], *, time_zone: str, time_format: str
) -> list[StructuredTool]:
    """Create only the explicitly enabled tools for this call."""
    tools: list[StructuredTool] = []
    if "calculator" in selected:
        async def calculator_tool(expression: str) -> str:
            return json.dumps(calculate(expression), separators=(",", ":"))

        tools.append(
            StructuredTool.from_function(
                coroutine=calculator_tool,
                name="calculator",
                description=(
                    "Evaluate a simple arithmetic expression. Use for numeric calculations; "
                    "expressions cannot access Python or the network."
                ),
                args_schema=CalculatorInput,
            )
        )
    if "time" in selected:
        async def time_tool(timestamp: str) -> str:
            result: dict[str, Any] = format_time(timestamp, time_zone, time_format)
            return json.dumps(result, separators=(",", ":"))

        tools.append(
            StructuredTool.from_function(
                coroutine=time_tool,
                name="time",
                description=(
                    "Convert an ISO 8601 timestamp that includes an offset into the configured "
                    f"time zone {time_zone!r}, using format {time_format!r}. Returns formatted "
                    "local time and an ISO 8601 value with an explicit offset."
                ),
                args_schema=TimeInput,
            )
        )
    return tools
