from cost_guard_mcp.pricing.runtime_scaling import scale_runtime_hours

_BASELINE_HOURS = 30 / 3600


def test_scale_runtime_returns_baseline_for_none_bytes():
    assert scale_runtime_hours(None, baseline_hours=_BASELINE_HOURS) == _BASELINE_HOURS


def test_scale_runtime_returns_baseline_under_10gb():
    assert scale_runtime_hours(5 * 1024**3, baseline_hours=_BASELINE_HOURS) == _BASELINE_HOURS


def test_scale_runtime_multiplies_4x_between_10gb_and_100gb():
    assert scale_runtime_hours(50 * 1024**3, baseline_hours=_BASELINE_HOURS) == _BASELINE_HOURS * 4


def test_scale_runtime_multiplies_20x_between_100gb_and_1tb():
    assert (
        scale_runtime_hours(500 * 1024**3, baseline_hours=_BASELINE_HOURS) == _BASELINE_HOURS * 20
    )


def test_scale_runtime_multiplies_100x_over_1tb():
    assert scale_runtime_hours(5 * 1024**4, baseline_hours=_BASELINE_HOURS) == _BASELINE_HOURS * 100


def test_scale_runtime_never_returns_less_than_baseline():
    for size_bytes in (0, 1, 1024, 1024**3, 1024**4, 10 * 1024**5):
        assert scale_runtime_hours(size_bytes, baseline_hours=_BASELINE_HOURS) >= _BASELINE_HOURS
