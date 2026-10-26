"""成本记账单元测试。"""

from polygate.cost import CostTracker, compute_cost


def test_compute_cost():
    pricing = {"m": {"input": 2, "output": 8}}
    assert compute_cost(1_000_000, 0, pricing, "m") == 2.0
    assert compute_cost(0, 1_000_000, pricing, "m") == 8.0
    assert compute_cost(500_000, 250_000, pricing, "m") == 3.0  # 1.0 + 2.0


def test_compute_cost_default():
    pricing = {"*": {"input": 10, "output": 10}}
    assert compute_cost(100_000, 100_000, pricing, "unknown") == 2.0


def test_compute_cost_no_pricing():
    assert compute_cost(100, 100, {}, "m") == 0.0


def test_cost_tracker(tmp_path):
    c = CostTracker(str(tmp_path / "c.db"))
    c.add("key1", "m", 1_000_000, 0, 2.0)
    c.add("key1", "m2", 0, 1_000_000, 8.0)
    c.add("key2", "m", 100, 100, 0.1)
    s = c.summary()
    assert abs(s["total"] - 10.1) < 1e-9
    keys = {r["key"]: r["cost"] for r in s["by_key"]}
    assert abs(keys["key1"] - 10.0) < 1e-9
    assert abs(keys["key2"] - 0.1) < 1e-9
    models = {r["model"]: r["cost"] for r in s["by_model"]}
    assert abs(models["m"] - 2.1) < 1e-9
    assert abs(models["m2"] - 8.0) < 1e-9
    c.close()
