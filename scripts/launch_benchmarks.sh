#!/bin/bash
# Launch the benchmark runner fully detached
cd "/Users/dawsonblock/Downloads/new builds/rfsn-agent"
nohup bash scripts/run_all_benchmarks.sh </dev/null >/tmp/bench_all.log 2>&1 &
echo "Benchmark runner launched as PID $!"
echo "Monitor with: tail -f /tmp/bench_all.log"
