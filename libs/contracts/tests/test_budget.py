from datetime import UTC, datetime

from huddle_contracts import budget as b

D = 86_400


def t(h: int, m: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 1, day, h, m, tzinfo=UTC)


def test_window_is_fixed_and_aligned() -> None:
    assert b.window_start(t(15), D) == t(0)
    assert b.window_end(t(15), D) == t(0, day=2)


def test_roll_resets_only_at_boundary() -> None:
    start = b.window_start(t(1), D)
    assert b.roll(t(23, 59), start, 400_000, D) == (start, 400_000)
    assert b.roll(t(0, day=2), start, 400_000, D) == (t(0, day=2), 0)


def test_no_gradual_refill() -> None:
    start = b.window_start(t(1), D)
    _, used = b.roll(t(20), start, 100_000, D)
    assert used == 100_000


def test_used_pct_rounds_up_and_clamps() -> None:
    assert b.used_pct(1, 500_000) == 1
    assert b.used_pct(0, 500_000) == 0
    assert b.used_pct(600_000, 500_000) == 100


def test_resting_when_less_than_one_turn_left() -> None:
    assert not b.is_resting(0, 500_000)
    assert b.is_resting(500_000 - b.FULL_TURN_TOKENS + 1, 500_000)
