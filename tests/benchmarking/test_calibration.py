"""Small-grid calibration utility tests."""

from __future__ import annotations

import pytest

from scripts.calibrate_thresholds import (
    expand_parameter_grid,
    select_best_index,
)


def test_parameter_grid_order_is_stable() -> None:
    combinations = expand_parameter_grid(
        {"z": [2, 1], "a": [True, False]},
        maximum_combinations=4,
    )

    assert combinations == [
        {"a": True, "z": 2},
        {"a": True, "z": 1},
        {"a": False, "z": 2},
        {"a": False, "z": 1},
    ]


def test_grid_limit_prevents_accidental_large_search() -> None:
    with pytest.raises(ValueError, match="limit"):
        expand_parameter_grid(
            {"a": [1, 2, 3], "b": [1, 2]},
            maximum_combinations=5,
        )


def test_objective_selection_handles_maximize_minimize_and_failures() -> None:
    assert select_best_index([0.4, None, 0.8], minimize=False) == 2
    assert select_best_index([0.4, None, 0.2], minimize=True) == 2
