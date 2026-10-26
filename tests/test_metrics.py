"""Prometheus 指标单元测试。"""

from polygate.metrics import Metrics


def test_counter_render():
    m = Metrics()
    m.inc("polygate_requests_total", {"model": "m", "status": "ok"})
    m.inc("polygate_requests_total", {"model": "m", "status": "ok"})
    m.inc("polygate_errors_total", {"model": "m"})
    text = m.render()
    assert 'polygate_requests_total{model="m",status="ok"} 2' in text
    assert 'polygate_errors_total{model="m"} 1' in text


def test_histogram_render():
    m = Metrics()
    m.observe("polygate_request_duration_seconds", 0.2, {"model": "m"})
    m.observe("polygate_request_duration_seconds", 5.0, {"model": "m"})
    text = m.render()
    assert 'polygate_request_duration_seconds_count{model="m"} 2' in text
    assert 'polygate_request_duration_seconds_bucket{le="0.25",model="m"} 1' in text
    assert 'polygate_request_duration_seconds_bucket{le="+Inf",model="m"} 2' in text


def test_extra_gauges():
    m = Metrics()
    text = m.render(extra_gauges=[("polygate_up", {}, 1.0)])
    assert "polygate_up 1" in text
