"""Guards the repository layout defined in CLAUDE.md section 5.

Every package named in the architecture must be importable from the repository
root. A renamed directory or a broken ``__init__.py`` fails here, at lint time,
rather than at deploy time.
"""

import importlib

import pytest

ARCHITECTURE_PACKAGES = [
    "lib",
    "services.agent",
    "services.agent.llm",
    "services.agent.output_guard",
    "services.inventory",
    "services.pricing",
    "services.worker",
]


@pytest.mark.parametrize("package_name", ARCHITECTURE_PACKAGES)
def test_architecture_package_is_importable(package_name: str) -> None:
    """Each package in ARCHITECTURE_PACKAGES resolves to a real module."""
    module = importlib.import_module(package_name)

    assert module.__name__ == package_name
