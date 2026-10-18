"""Tiny hyperfine stand-in for measuring command latency.

Usage::

    python3 bench.py "cmd one" "cmd two" ... [-n RUNS]

Each command string is split with :func:`shlex.split` and executed directly
(no shell), so the timing contains only process creation plus the command
itself. Every command gets 5 untimed warm-up runs, then ``RUNS`` timed runs
(default 100) with stdout discarded. Prints mean, median, min and p95 in
milliseconds. A command that exits non-zero aborts the benchmark
(``CalledProcessError``).

Used by ``make bench`` when ``hyperfine`` is not installed, and by test.sh.
"""

import argparse
import shlex
import statistics
import subprocess
import time

p = argparse.ArgumentParser()
p.add_argument("commands", nargs="+")
p.add_argument("-n", "--runs", type=int, default=100)
args = p.parse_args()

for cmd in args.commands:
    argv = shlex.split(cmd)
    for _ in range(5):  # warm-up
        subprocess.run(argv, stdout=subprocess.DEVNULL, check=True)
    samples = []
    for _ in range(args.runs):
        t0 = time.perf_counter()
        subprocess.run(argv, stdout=subprocess.DEVNULL, check=True)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    print(f"{cmd}\n  mean {statistics.mean(samples):6.2f} ms   median {statistics.median(samples):6.2f} ms"
          f"   min {samples[0]:6.2f} ms   p95 {samples[int(len(samples) * 0.95) - 1]:6.2f} ms   ({args.runs} runs)")
