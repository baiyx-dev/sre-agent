import argparse
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request


def percentile(values: list[float], percentage: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentage) - 1))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded SRE Agent load smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--max-p95-ms", type=float, default=1000.0)
    args = parser.parse_args()
    request_count = max(1, min(args.requests, 10_000))
    concurrency = max(1, min(args.concurrency, 100))
    url = args.base_url.rstrip("/") + "/services/"

    def run_one(_: int) -> tuple[int, float]:
        outgoing = request.Request(
            url,
            headers={"X-SRE-API-Key": args.api_key, "Accept": "application/json"},
        )
        started = time.perf_counter()
        try:
            with request.urlopen(outgoing, timeout=10) as response:
                response.read()
                status = response.status
        except Exception:
            status = 0
        return status, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_one, index) for index in range(request_count)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started
    latencies = [duration for _, duration in results]
    successes = sum(1 for status, _ in results if status == 200)
    summary = {
        "requests": request_count,
        "concurrency": concurrency,
        "successes": successes,
        "errors": request_count - successes,
        "success_rate_pct": round(successes * 100 / request_count, 2),
        "p50_ms": round(percentile(latencies, 0.50), 2),
        "p95_ms": round(percentile(latencies, 0.95), 2),
        "max_ms": round(max(latencies, default=0), 2),
        "throughput_rps": round(request_count / elapsed, 2) if elapsed else 0,
    }
    summary["passed"] = successes == request_count and summary["p95_ms"] <= args.max_p95_ms
    print(json.dumps(summary, indent=2))
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
