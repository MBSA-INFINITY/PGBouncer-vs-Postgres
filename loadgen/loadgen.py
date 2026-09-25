#!/usr/bin/env python3
"""Custom Postgres/PGBouncer load generator for the pgbouncer-poc comparative test.

Same tool, same workload - only TARGET_HOST changes between the "direct to
Postgres" run and the "through PGBouncer" run, so results are comparable.
Config is env-var driven (for the k8s Job manifest) with CLI overrides for
local runs.
"""
import argparse
import json
import os
import random
import signal
import statistics
import threading
import time
from dataclasses import dataclass, field

import psycopg2


def env(*names, default=None):
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return default


def parse_args():
    p = argparse.ArgumentParser(description="Postgres/PGBouncer load generator")
    p.add_argument("--host", default=env("TARGET_HOST", "PGHOST", default="pg-poc"))
    p.add_argument("--port", type=int, default=int(env("TARGET_PORT", "PGPORT", default="5432")))
    p.add_argument("--dbname", default=env("DB_NAME", "POSTGRES_DB", "PGDATABASE", default="pocdb"))
    p.add_argument("--user", default=env("DB_USER", "POSTGRES_USER", "PGUSER", default="poc_user"))
    p.add_argument("--password", default=env("DB_PASSWORD", "POSTGRES_PASSWORD", "PGPASSWORD", default=""))
    p.add_argument("--concurrency", type=int, default=int(env("CONCURRENCY", default="20")))
    p.add_argument("--duration", type=int, default=int(env("DURATION_SECONDS", default="120")))
    p.add_argument("--ramp-up", type=int, default=int(env("RAMP_UP_SECONDS", default="0")))
    p.add_argument(
        "--connection-mode", choices=["persistent", "churn"],
        default=env("CONNECTION_MODE", default="persistent"),
        help="persistent = one connection per worker for the whole run; "
             "churn = open/close a brand-new connection for every transaction",
    )
    p.add_argument(
        "--workload", choices=["read_only", "read_write", "mixed"],
        default=env("WORKLOAD", default="read_only"),
    )
    p.add_argument("--write-ratio", type=float, default=float(env("WRITE_RATIO", default="0.2")))
    p.add_argument("--row-count", type=int, default=int(env("TABLE_ROWS", default="100000")))
    p.add_argument(
        "--init-schema", action="store_true",
        default=env("INIT_SCHEMA", default="true").lower() in ("1", "true", "yes"),
        help="create/seed the loadgen_accounts table if missing before the run",
    )
    p.add_argument("--report-interval", type=int, default=int(env("REPORT_INTERVAL_SECONDS", default="5")))
    p.add_argument("--label", default=env("TEST_LABEL", default=""))
    p.add_argument("--connect-timeout", type=int, default=int(env("CONNECT_TIMEOUT_SECONDS", default="10")))
    p.add_argument("--statement-timeout-ms", type=int, default=int(env("STATEMENT_TIMEOUT_MS", default="30000")))
    return p.parse_args()


@dataclass
class WorkerStats:
    latencies_ms: list = field(default_factory=list)
    connect_latencies_ms: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    tx_count: int = 0


def connect(args):
    conn = psycopg2.connect(
        host=args.host, port=args.port, dbname=args.dbname,
        user=args.user, password=args.password,
        connect_timeout=args.connect_timeout,
    )
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(args.statement_timeout_ms)}")
    conn.commit()
    return conn


def choose_kind(args):
    if args.workload == "read_only":
        return "read"
    if args.workload == "read_write":
        return "write"
    return "write" if random.random() < args.write_ratio else "read"


def run_transaction(conn, args):
    kind = choose_kind(args)
    account_id = random.randint(1, args.row_count)
    t0 = time.perf_counter()
    try:
        with conn.cursor() as cur:
            if kind == "read":
                cur.execute("SELECT balance FROM loadgen_accounts WHERE id = %s", (account_id,))
                cur.fetchone()
            else:
                delta = random.choice([-1, 1]) * round(random.uniform(0.01, 25.00), 2)
                cur.execute(
                    "UPDATE loadgen_accounts SET balance = balance + %s, updated_at = now() WHERE id = %s",
                    (delta, account_id),
                )
        conn.commit()
        return True, (time.perf_counter() - t0) * 1000, None
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, (time.perf_counter() - t0) * 1000, f"query:{type(exc).__name__}"


def worker_persistent(stats, stop_event, args):
    conn = None
    try:
        t0 = time.perf_counter()
        conn = connect(args)
        stats.connect_latencies_ms.append((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        stats.errors.append(f"connect:{type(exc).__name__}")
        return

    while not stop_event.is_set():
        ok, elapsed_ms, err = run_transaction(conn, args)
        stats.tx_count += 1
        if ok:
            stats.latencies_ms.append(elapsed_ms)
            continue
        stats.errors.append(err)
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn = connect(args)
        except Exception as exc2:
            stats.errors.append(f"reconnect:{type(exc2).__name__}")
            time.sleep(0.5)

    try:
        conn.close()
    except Exception:
        pass


def worker_churn(stats, stop_event, args):
    while not stop_event.is_set():
        t0 = time.perf_counter()
        try:
            conn = connect(args)
        except Exception as exc:
            stats.errors.append(f"connect:{type(exc).__name__}")
            stats.tx_count += 1
            time.sleep(0.05)
            continue
        connect_ms = (time.perf_counter() - t0) * 1000
        stats.connect_latencies_ms.append(connect_ms)

        ok, query_ms, err = run_transaction(conn, args)
        stats.tx_count += 1
        if ok:
            stats.latencies_ms.append(connect_ms + query_ms)
        else:
            stats.errors.append(err)

        try:
            conn.close()
        except Exception:
            pass


def init_schema(args):
    conn = None
    for attempt in range(1, 31):
        try:
            conn = connect(args)
            break
        except Exception as exc:
            print(f"[init] waiting for database (attempt {attempt}): {exc}", flush=True)
            time.sleep(2)
    if conn is None:
        raise SystemExit("Could not connect to database to initialize schema")

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS loadgen_accounts (
                id BIGINT PRIMARY KEY,
                balance NUMERIC(12, 2) NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()
        cur.execute("SELECT count(*) FROM loadgen_accounts")
        (existing,) = cur.fetchone()
        if existing < args.row_count:
            print(f"[init] seeding loadgen_accounts up to {args.row_count} rows (has {existing})", flush=True)
            cur.execute(
                """
                INSERT INTO loadgen_accounts (id, balance)
                SELECT g, round((random() * 1000)::numeric, 2)
                FROM generate_series(%s, %s) AS g
                ON CONFLICT (id) DO NOTHING
                """,
                (existing + 1, args.row_count),
            )
            conn.commit()
    conn.close()


def percentile(data, pct):
    if not data:
        return None
    data_sorted = sorted(data)
    idx = min(len(data_sorted) - 1, int(round(pct / 100 * (len(data_sorted) - 1))))
    return round(data_sorted[idx], 2)


def fmt(v):
    return f"{v:.2f}" if v is not None else "n/a"


def print_progress(stats_list, start_time):
    elapsed = time.time() - start_time
    all_lat = []
    total_tx = 0
    total_err = 0
    for s in stats_list:
        all_lat.extend(s.latencies_ms)
        total_tx += s.tx_count
        total_err += len(s.errors)
    tps = (len(all_lat) / elapsed) if elapsed > 0 else 0
    print(
        f"[progress] t={elapsed:6.1f}s tx={total_tx:>8} tps={tps:7.1f} "
        f"p50={fmt(percentile(all_lat, 50))}ms p95={fmt(percentile(all_lat, 95))}ms "
        f"p99={fmt(percentile(all_lat, 99))}ms errors={total_err}",
        flush=True,
    )


def build_summary(stats_list, start_time, args):
    all_lat, all_connect_lat, error_counter = [], [], {}
    total_tx = 0
    for s in stats_list:
        all_lat.extend(s.latencies_ms)
        all_connect_lat.extend(s.connect_latencies_ms)
        total_tx += s.tx_count
        for e in s.errors:
            error_counter[e] = error_counter.get(e, 0) + 1
    elapsed = time.time() - start_time

    return {
        "label": args.label,
        "target_host": args.host,
        "target_port": args.port,
        "connection_mode": args.connection_mode,
        "workload": args.workload,
        "concurrency": args.concurrency,
        "requested_duration_s": args.duration,
        "elapsed_s": round(elapsed, 2),
        "total_transactions": total_tx,
        "successful_transactions": len(all_lat),
        "total_errors": sum(error_counter.values()),
        "error_breakdown": error_counter,
        "throughput_tps": round(len(all_lat) / elapsed, 2) if elapsed > 0 else 0,
        "latency_ms": {
            "min": round(min(all_lat), 2) if all_lat else None,
            "mean": round(statistics.fmean(all_lat), 2) if all_lat else None,
            "p50": percentile(all_lat, 50),
            "p90": percentile(all_lat, 90),
            "p95": percentile(all_lat, 95),
            "p99": percentile(all_lat, 99),
            "max": round(max(all_lat), 2) if all_lat else None,
        },
        "connect_latency_ms": {
            "min": round(min(all_connect_lat), 2) if all_connect_lat else None,
            "mean": round(statistics.fmean(all_connect_lat), 2) if all_connect_lat else None,
            "p50": percentile(all_connect_lat, 50),
            "p95": percentile(all_connect_lat, 95),
            "max": round(max(all_connect_lat), 2) if all_connect_lat else None,
        },
    }


def start_worker(worker_fn, stats, stop_event, args, delay):
    if delay:
        time.sleep(delay)
    worker_fn(stats, stop_event, args)


def main():
    args = parse_args()
    if args.init_schema:
        init_schema(args)

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        print(f"[loadgen] received signal {signum}, stopping...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    stats_list = [WorkerStats() for _ in range(args.concurrency)]
    worker_fn = worker_churn if args.connection_mode == "churn" else worker_persistent

    print(
        f"[loadgen] label={args.label!r} target={args.host}:{args.port}/{args.dbname} "
        f"mode={args.connection_mode} workload={args.workload} concurrency={args.concurrency} "
        f"duration={args.duration}s ramp_up={args.ramp_up}s",
        flush=True,
    )

    start_time = time.time()
    threads = []
    for i in range(args.concurrency):
        # Stagger worker start across the ramp-up window to avoid a synchronized connection stampede.
        delay = (args.ramp_up * i / max(args.concurrency, 1)) if args.ramp_up else 0
        t = threading.Thread(
            target=start_worker, args=(worker_fn, stats_list[i], stop_event, args, delay), daemon=True
        )
        t.start()
        threads.append(t)

    deadline = start_time + args.ramp_up + args.duration
    try:
        while time.time() < deadline:
            time.sleep(min(args.report_interval, max(0.1, deadline - time.time())))
            print_progress(stats_list, start_time)
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=15)

    summary = build_summary(stats_list, start_time, args)
    print("RESULT_JSON: " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
