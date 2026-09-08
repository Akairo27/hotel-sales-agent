from __future__ import annotations

from services.agent.llm.dispatch import CHECK_AVAILABILITY_TOOL, GET_QUOTE_TOOL
from services.agent.llm.tools import AGENT_TOOLS, CHECK_AVAILABILITY, GET_QUOTE

_EXPECTED_STAY_ARGS = {"hotel_id", "room_type_id", "check_in", "check_out", "rooms"}


def test_check_availability_name_matches_dispatch_routing() -> None:
    assert CHECK_AVAILABILITY.name == CHECK_AVAILABILITY_TOOL


def test_get_quote_name_matches_dispatch_routing() -> None:
    assert GET_QUOTE.name == GET_QUOTE_TOOL


def test_check_availability_required_args_match_what_dispatch_parses() -> None:
    assert CHECK_AVAILABILITY.parameters is not None
    assert set(CHECK_AVAILABILITY.parameters.required or []) == _EXPECTED_STAY_ARGS
    assert set((CHECK_AVAILABILITY.parameters.properties or {}).keys()) == (
        _EXPECTED_STAY_ARGS
    )


def test_get_quote_required_args_match_what_dispatch_parses() -> None:
    assert GET_QUOTE.parameters is not None
    assert set(GET_QUOTE.parameters.required or []) == _EXPECTED_STAY_ARGS
    assert set((GET_QUOTE.parameters.properties or {}).keys()) == _EXPECTED_STAY_ARGS


def test_agent_tools_declares_exactly_the_two_read_only_tools() -> None:
    """PLAN.md's المرحلة ٤ scopes the agent to check_availability and
    get_quote in this PR — search_alternatives has no implementation
    anywhere in the repo yet (see dispatch.py's module docstring) and
    must not be declared until it does."""
    assert len(AGENT_TOOLS) == 1
    names = {
        declaration.name for declaration in AGENT_TOOLS[0].function_declarations or []
    }
    assert names == {CHECK_AVAILABILITY_TOOL, GET_QUOTE_TOOL}
