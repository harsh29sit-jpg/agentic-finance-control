"""Observability: Prometheus exposition format, request metrics, agent metrics."""
import os
import re

import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ANALYST = ("analyst@recon.io", "analyst123")


import functools
@functools.lru_cache(maxsize=1)
def _token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": ANALYST[0], "password": ANALYST[1]}, timeout=30)
    return r.json()["token"]

def _login():
    return {"Authorization": f"Bearer {_token()}"}


class TestPrometheusEndpoint:
    def test_metrics_exposes_http_and_uptime_series(self):
        requests.get(f"{BASE_URL}/api/health", timeout=10)
        _login()
        raw = requests.get(f"{BASE_URL}/api/metrics", timeout=10).text

        assert "controltower_uptime_seconds" in raw
        assert "# TYPE http_requests_total counter" in raw
        m = re.search(r'http_requests_total\{[^}]*path="/api/health"[^}]*\}\s+(\d+)', raw)
        assert m and int(m.group(1)) >= 1
        assert "# TYPE http_request_duration_seconds histogram" in raw
        assert 'http_request_duration_seconds_count' in raw

    def test_histogram_buckets_cumulative_within_one_series(self):
        _login()  # ensure a login-series exists
        raw = requests.get(f"{BASE_URL}/api/metrics", timeout=10).text
        # take ONE full series (same label set) and verify monotonic buckets
        series_lines = [l for l in raw.splitlines()
                        if l.startswith("http_request_duration_seconds_bucket{")
                        and 'method="POST"' in l and "/api/auth/login" in l]
        if len(series_lines) < 2:
            pytest.skip("login histogram not populated yet")
        counts = []
        for line in series_lines:
            labels, value = line.rsplit("}", 1)
            assert 'le="' in labels
            counts.append(int(value))
        assert counts == sorted(counts)

    def test_agent_loop_invocations_recorded(self, monkeypatch):
        from agents import providers

        class Fake:
            async def __call__(self, system, prompt):
                import json as j
                return j.dumps({"action": "final", "answer": "ok",
                                "cited_records": [], "suggested_next_action": "-"})

        monkeypatch.setattr(providers, "_SEND", Fake())
        h = _login()
        r = requests.post(f"{BASE_URL}/api/copilot/agent", headers=h,
                          files={"question": (None, "hello agent")}, timeout=60)
        assert r.status_code == 200
        inv = requests.get(f"{BASE_URL}/api/model-invocations?limit=5",
                           headers=h, timeout=30).json()
        assert any(i["agent"] == "agent_loop" for i in inv)

    def test_agent_tool_counter_counts_actions(self, monkeypatch):
        """Full action-metric path exercised in test_agent_console; here we
        verify the counter series renders once ANY agent tool has run."""
        from agents import providers

        class Fake:
            def __init__(self):
                self.n = 0

            async def __call__(self, system, prompt):
                import json as j
                self.n += 1
                if self.n == 1:
                    return j.dumps({"action": "tool",
                                    "tool": "query_batches", "args": {"limit": 1},
                                    "thought": "peek"})
                return j.dumps({"action": "final", "answer": "done",
                                "cited_records": [], "suggested_next_action": "-"})

        fake = Fake()
        monkeypatch.setattr(providers, "_SEND", fake)
        h = _login()
        requests.post(f"{BASE_URL}/api/copilot/agent", headers=h,
                      files={"question": (None, "show batches")}, timeout=60)
        raw = requests.get(f"{BASE_URL}/api/metrics", timeout=10).text
        assert re.search(r'agent_tool_calls_total\{[^}]*tool="query_batches"[^}]*\}\s+[1-9]', raw)
