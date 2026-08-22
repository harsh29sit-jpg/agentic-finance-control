"""Minimal in-process Prometheus-style metrics registry (zero dependencies).

Counters and histograms live per-process; scrape /api/metrics from each
instance or aggregate externally (Prometheus handles multi-target natively).
"""
import time
from collections import defaultdict

_START = time.time()
_counters = defaultdict(float)
# key=(name, labels_tuple) -> {"buckets": {le: count_cumulative}, "sum": f, "count": n}
_histograms = defaultdict(lambda: {"buckets": defaultdict(int), "sum": 0.0, "count": 0})

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SLOW_REQUEST_S = 1.0


def _key(name, labels):
    return (name, tuple(sorted((labels or {}).items())))


def _fmt_labels(labels, extra=None):
    pairs = dict(labels or {})
    if extra:
        pairs.update(extra)
    if not pairs:
        return ""
    clean = {k: str(v).replace('"', "'") for k, v in pairs.items()}
    # canonical ordering: user labels sorted, then `le` last
    items = [(k, clean[k]) for k in sorted(clean) if k != "le"]
    if "le" in clean:
        items.append(("le", clean["le"]))
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"


def inc(name: str, value: float = 1.0, labels: dict | None = None):
    _counters[_key(name, labels)] += value


def observe(name: str, value: float, labels: dict | None = None):
    h = _histograms[_key(name, labels)]
    for b in BUCKETS:
        if value <= b:
            h["buckets"][b] += 1
    h["sum"] += value
    h["count"] += 1


def record_request(method, path_template, status, duration_s):
    labels = {"method": method, "path": path_template, "status": str(status)}
    inc("http_requests_total", 1, labels)
    observe("http_request_duration_seconds", duration_s,
            {"method": method, "path": path_template})


def record_agent_tool(tool, ok, state_changed=False):
    inc("agent_tool_calls_total", 1,
        {"tool": tool, "ok": "true" if ok else "false"})
    if state_changed and ok:
        inc("agent_state_changes_total", 1, {"tool": tool})


def render() -> str:
    lines = [
        "# TYPE controltower_uptime_seconds gauge",
        f"controltower_uptime_seconds {time.time() - _START:.3f}",
    ]
    for (name, labels), v in sorted(_counters.items()):
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name}{_fmt_labels(dict(labels))} {v}")

    hist_names = sorted({n for (n, _l) in _histograms})
    for name in hist_names:
        lines.append(f"# TYPE {name} histogram")
        keys = [(n, l) for (n, l) in _histograms if n == name]
        for _n, labels in sorted(keys, key=lambda k: k[1]):
            h = _histograms[(name, labels)]
            base = dict(labels)
            for b in BUCKETS:
                le = _fmt_labels(base, {"le": repr(b)})
                lines.append(f"{name}_bucket{le} {h['buckets'].get(b, 0)}")
            inf = _fmt_labels(base, {"le": "+Inf"})
            lines.append(f"{name}_bucket{inf} {h['count']}")
            lines.append(f"{name}_sum{_fmt_labels(base)} {h['sum']:.6f}")
            lines.append(f"{name}_count{_fmt_labels(base)} {h['count']}")
    return "\n".join(lines) + "\n"
