"""Benchmark entrypoint: agent pipeline vs single-prompt baseline over injected
faults, plus the summarization ablation.

Default is MOCK mode: fully offline (fault-aware mock sandbox + deterministic
heuristic mock model), zero Docker, zero tokens — use it for development and
CI. `--real` is the single real-model entry point for the final run: it needs
DASHSCOPE_API_KEY and a running Docker daemon, and it spends actual tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from autopilot.benchmark.mockenv import HeuristicMockClient, MockWorld
from autopilot.benchmark.report import render_markdown, write_artifacts
from autopilot.benchmark.runner import run_benchmark
from autopilot.harness.synthetic import FAULT_IDS

DEFAULT_OUT = Path("benchmark_results")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m autopilot.benchmark",
        description="Agent pipeline vs single-prompt baseline over seeded faults.",
    )
    parser.add_argument(
        "--real", action="store_true",
        help="FINAL-RUN ONLY: real Qwen models + real Docker sandbox "
             "(spends tokens; default is offline mock mode)")
    parser.add_argument(
        "--scenarios", default=",".join(FAULT_IDS),
        help=f"comma-separated fault ids (default: all — {','.join(FAULT_IDS)})")
    parser.add_argument(
        "--no-ablation", action="store_true",
        help="skip the summarized-vs-raw context ablation runs")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help=f"artifact directory (default: {DEFAULT_OUT}/)")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    fault_ids = [f.strip() for f in args.scenarios.split(",") if f.strip()]
    unknown = [f for f in fault_ids if f not in FAULT_IDS]
    if unknown:
        print(f"unknown fault ids: {unknown}; known: {FAULT_IDS}", file=sys.stderr)
        return 2

    if args.real:
        from autopilot.benchmark.liveenv import LiveWorld
        from autopilot.llm.client import QwenClient

        client = QwenClient()  # raises unless DASHSCOPE_API_KEY is set
        if client.config.mock_mode:
            print("--real conflicts with AUTOPILOT_MOCK_LLM=1; unset it for the "
                  "final run", file=sys.stderr)
            return 2
        world_factory, mode = LiveWorld, "real"
    else:
        client = HeuristicMockClient()
        world_factory, mode = MockWorld, "mock"

    report, traces = await run_benchmark(
        fault_ids, client=client, world_factory=world_factory,
        mode=mode, ablation=not args.no_ablation,
    )
    written = write_artifacts(report, traces, args.out)
    print(render_markdown(report))
    print(f"artifacts: {written['results']}, {written['report']}, "
          f"{len(traces)} traces under {args.out / 'traces'}/")
    client.meter.print_session_summary()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
