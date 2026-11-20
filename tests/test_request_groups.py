from polygate.requestlog import RequestLog


def test_request_groups_keep_failover_chain(tmp_path):
    log = RequestLog(str(tmp_path / "requests.db"))
    try:
        log.log(
            req_id="attempt-1",
            logical_id="logical-1",
            endpoint="/v1/chat/completions",
            model="public",
            provider="primary",
            key_label="k1...0001",
            client="client-a",
            status="error",
            status_code=429,
            latency_ms=12,
            request={"model": "public", "messages": [{"role": "user", "content": "hi"}]},
            error="rate limited",
            upstream_model="upstream-1",
        )
        log.log(
            req_id="attempt-2",
            logical_id="logical-1",
            endpoint="/v1/chat/completions",
            model="public",
            provider="fallback",
            key_label="k2...0002",
            client="client-a",
            status="ok",
            status_code=200,
            latency_ms=25,
            request={"model": "public", "messages": [{"role": "user", "content": "hi"}]},
            response={"choices": [{"message": {"content": "ok"}}]},
            upstream_model="upstream-2",
        )

        groups = log.groups(limit=20)
        assert len(groups) == 1
        group = groups[0]
        assert group["group_id"] == "logical-1"
        assert group["attempt_count"] == 2
        assert group["providers"] == ["primary", "fallback"]
        assert group["status"] == "ok"
        assert group["latency_ms"] == 37

        assert log.groups(status="error") == []
        assert len(log.groups(status="ok")) == 1

        detail = log.group("logical-1")
        assert detail["request"]["messages"][0]["content"] == "hi"
        assert detail["response"]["choices"][0]["message"]["content"] == "ok"
        assert [attempt["req_id"] for attempt in detail["attempts"]] == ["attempt-1", "attempt-2"]
    finally:
        log.close()


def test_legacy_requests_without_logical_id_remain_individual(tmp_path):
    log = RequestLog(str(tmp_path / "legacy.db"))
    try:
        for index in range(2):
            log.log(
                req_id=f"legacy-{index}",
                endpoint="/v1/chat/completions",
                model="m",
                provider="p",
                key_label="k",
                status="ok",
                status_code=200,
                latency_ms=1,
            )
        groups = log.groups(limit=10)
        assert len(groups) == 2
        assert {group["group_id"] for group in groups} == {"legacy-0", "legacy-1"}
    finally:
        log.close()
