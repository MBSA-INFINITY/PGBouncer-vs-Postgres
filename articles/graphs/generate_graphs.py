#!/usr/bin/env python3
"""Parse loadgen log files and plot tx-vs-time / tps-vs-time comparison charts.

Each log file may contain multiple back-to-back runs (e.g. a direct-to-Postgres
run followed by a via-PGBouncer run in the same file, as produced by running
both loadgen Jobs and concatenating their `oc logs` output). Every run is
plotted as its own labeled series on the same axes so direct vs. pooled runs
can be compared directly in one chart.

Usage:
    python generate_graphs.py <logfile1> [<logfile2> ...] [--outdir DIR]
"""
import argparse
import re
from pathlib import Path

import matplotlib.pyplot as plt

RUN_START_RE = re.compile(
    r"^\[loadgen\]\s+label='(?P<label>[^']*)'\s+target=(?P<target>\S+)\s+"
    r"mode=(?P<mode>\S+)\s+workload=(?P<workload>\S+)\s+concurrency=(?P<concurrency>\d+)\s+"
    r"duration=(?P<duration>\d+)s\s+ramp_up=(?P<ramp_up>\d+)s"
)
PROGRESS_RE = re.compile(
    r"^\[progress\]\s+t=\s*(?P<t>[\d.]+)s\s+tx=\s*(?P<tx>\d+)\s+tps=\s*(?P<tps>[\d.]+)\s+"
    r"p50=(?P<p50>[\d.]+)ms\s+p95=(?P<p95>[\d.]+)ms\s+p99=(?P<p99>[\d.]+)ms\s+errors=(?P<errors>\d+)"
)


class Run:
    def __init__(self, label, meta):
        self.label = label
        self.meta = meta
        self.t = []
        self.tx = []
        self.tps = []

    def add(self, t, tx, tps):
        self.t.append(t)
        self.tx.append(tx)
        self.tps.append(tps)


def parse_log(path: Path):
    runs = []
    current = None
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            m = RUN_START_RE.match(line)
            if m:
                current = Run(m.group("label"), m.groupdict())
                runs.append(current)
                continue
            m = PROGRESS_RE.match(line)
            if m and current is not None:
                current.add(float(m.group("t")), int(m.group("tx")), float(m.group("tps")))
    return runs


def plot_metric(runs, metric, ylabel, title, outfile):
    plt.figure(figsize=(10, 6))
    for run in runs:
        plt.plot(
            run.t, getattr(run, metric),
            linewidth=2,
            label=f"{run.label} (concurrency={run.meta['concurrency']})",
        )
    plt.xlabel("Elapsed time (s)")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig(outfile, dpi=150)
    plt.close()
    print(f"Saved {outfile}")


def main():
    parser = argparse.ArgumentParser(description="Generate tx-vs-time and tps-vs-time graphs from loadgen logs")
    parser.add_argument("logfiles", nargs="+", help="Path(s) to loadgen logs.txt files")
    parser.add_argument("--outdir", default="graphs_output", help="Directory to write PNG charts to")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for logfile in args.logfiles:
        path = Path(logfile)
        runs = parse_log(path)
        if not runs:
            print(f"No runs found in {path}")
            continue

        # results folder name (e.g. "50con-persistent-180s-20pgbcon") makes a good chart title
        stem = path.parent.name or path.stem

        plot_metric(
            runs, "tx", "Cumulative transactions",
            f"Cumulative Transactions vs Time — {stem}",
            outdir / f"{stem}_tx_vs_time.png",
        )
        plot_metric(
            runs, "tps", "Throughput (tx/sec)",
            f"Throughput vs Time — {stem}",
            outdir / f"{stem}_tps_vs_time.png",
        )


if __name__ == "__main__":
    main()
