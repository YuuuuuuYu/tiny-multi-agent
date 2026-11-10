"""Command line entry point."""

import argparse
import asyncio
import json
import math
import sys
from dataclasses import asdict

from .providers import COMMANDS, Agent
from .workflow import run


def agent_arg(value: str) -> Agent:
    provider, separator, model = value.partition(":")
    if provider not in COMMANDS and provider != "mock":
        raise argparse.ArgumentTypeError(f"unknown provider: {provider}")
    if separator and (not model or model.startswith("-") or any(c.isspace() for c in model)):
        raise argparse.ArgumentTypeError("expected PROVIDER or PROVIDER:MODEL")
    return Agent(provider, model or None)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected an integer greater than zero")
    return number


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("expected a finite number greater than zero")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="Draft, review and synthesize with local AI CLIs.")
    providers = ", ".join([*COMMANDS, "mock"])
    parser.add_argument("task", help="task text, or - to read stdin")
    parser.add_argument("--lead", required=True, type=agent_arg, help=f"PROVIDER[:MODEL] ({providers})")
    parser.add_argument("--reviewer", action="append", default=[], type=agent_arg,
                        help="PROVIDER[:MODEL], repeat for multiple reviewers")
    parser.add_argument("--jobs", type=positive_int, default=2, help="concurrent reviews (default 2)")
    parser.add_argument("--timeout", type=positive_seconds, default=120,
                        help="seconds per call (default 120)")
    parser.add_argument("--context-chars", type=positive_int, default=4000,
                        help="maximum characters per draft or review handed off (default 4000)")
    parser.add_argument("--json", action="store_true", help="emit the full report with step outputs")
    args = parser.parse_args()

    task = (sys.stdin.read() if args.task == "-" else args.task).strip()
    if not task:
        parser.error("task cannot be empty")
    if len(set(args.reviewer)) != len(args.reviewer):
        parser.error("use each reviewer at most once")

    try:
        report = asyncio.run(run(
            task, args.lead, args.reviewer, jobs=args.jobs,
            timeout=args.timeout, context_chars=args.context_chars,
        ))
    except KeyboardInterrupt:
        return 130

    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        for step in report.steps:
            status = f"failed ({step.error})" if step.error else "ok"
            print(f"{step.stage:9} {step.agent:20} {step.seconds:7.3f}s  "
                  f"{step.prompt_chars:5} prompt chars  {status}", file=sys.stderr)
        degraded = " (some reviews failed)" if report.degraded else ""
        print(f"{len(report.steps)} calls in {report.seconds:.3f}s{degraded}", file=sys.stderr)
        if report.answer is not None:
            print(report.answer)
    return 0 if report.answer is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
