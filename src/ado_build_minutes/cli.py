"""Command-line interface for ado-build-minutes."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import sys

try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
except ImportError:  # pragma: no cover - fallback keeps --help usable before optional runtime deps install.
    Console = None  # type: ignore[assignment]
    RichHandler = None  # type: ignore[assignment]

    class _NoopColumn:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    SpinnerColumn = _NoopColumn  # type: ignore[assignment]
    TextColumn = _NoopColumn  # type: ignore[assignment]
    TimeElapsedColumn = _NoopColumn  # type: ignore[assignment]

    class Progress:  # type: ignore[no-redef]
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "Progress":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            pass

        def add_task(self, description: str, total: object = None) -> int:
            print(description)
            return 0

        def update(self, task_id: int, description: str) -> None:
            print(description)

    class _PlainConsole:
        def print(self, *args: object, **kwargs: object) -> None:
            print(*args)

from .auth import make_auth_provider
from .billing import collect_billing
from .config import capped_concurrency, load_config, merge_orgs
from .doctor import run_doctor
from .http import AzureDevOpsHttpClient
from .jobrequests import collect_jobrequests
from .analytics import collect_analytics
from .models import CollectionResult
from .output import write_outputs
from .timeline import collect_timeline

VALID_SOURCES = {"analytics", "jobrequests", "timeline", "billing"}


def _date_arg(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _end_date_arg(value: str) -> datetime:
    parsed = _date_arg(value)
    return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)


def _split_sources(value: str) -> list[str]:
    if value == "all":
        return sorted(VALID_SOURCES)
    sources = [item.strip() for item in value.split(",") if item.strip()]
    invalid = sorted(set(sources) - VALID_SOURCES)
    if invalid:
        raise argparse.ArgumentTypeError(f"invalid source(s): {', '.join(invalid)}")
    return sources


def configure_logging(verbose: bool) -> None:
    """Configure structured-ish Rich logging for CLI runs."""
    handler = RichHandler(rich_tracebacks=verbose, show_path=verbose) if RichHandler else logging.StreamHandler()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[handler],
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="TOML config path (default: config.toml if present)")
    common.add_argument("--org", action="append", default=[], help="Azure DevOps org name; repeatable")
    common.add_argument("--orgs-file", help="Text file with one org per line")
    common.add_argument("--tenant", help="Tenant ID override for Microsoft Entra token acquisition")
    common.add_argument("--auth", choices=("entra", "pat"), default="entra", help="Auth mode; PAT reads AZURE_DEVOPS_EXT_PAT")
    common.add_argument("--concurrency", type=int, help="Concurrent HTTP requests, hard-capped at 20")
    common.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    parser = argparse.ArgumentParser(
        prog="ado-build-minutes",
        description="Extract Azure DevOps build minutes by runner type across organisations.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", parents=[common], help="Check auth and permissions before collection")
    doctor.add_argument("--source", default=["analytics"], type=_split_sources, help="Probe endpoints for comma-separated run sources")
    doctor.set_defaults(func=_doctor_command)
    check = sub.add_parser("check", parents=[common], help="Alias for doctor")
    check.add_argument("--source", default=["analytics"], type=_split_sources, help="Probe endpoints for comma-separated run sources")
    check.set_defaults(func=_doctor_command)

    run = sub.add_parser("run", parents=[common], help="Collect data and write reports")
    run.add_argument("--source", default=["analytics"], type=_split_sources, help="Comma-separated: analytics,jobrequests,timeline,billing or all")
    run.add_argument("--start", type=_date_arg, help="Inclusive UTC start date YYYY-MM-DD (default: 30 days ago)")
    run.add_argument("--end", type=_end_date_arg, help="Inclusive UTC end date YYYY-MM-DD (default: today)")
    run.add_argument("--format", choices=("csv", "json", "markdown", "all"), default="markdown", help="Output format")
    run.add_argument("--out-dir", default="./out", help="Output directory")
    run.add_argument("--state-file", default=".ado-build-minutes-state.json", help="Timeline checkpoint file")
    run.add_argument("--jobrequest-count", type=int, default=1000, help="completedRequestCount per pool for jobrequests")
    run.add_argument("--actions-cost-model", action="store_true", help="Estimate GitHub Actions costs using config rates")
    run.set_defaults(func=_run_command)
    return parser


def _load_runtime(args: argparse.Namespace) -> tuple[Console, list[str], int, object, object]:
    configure_logging(args.verbose)
    console = Console() if Console else _PlainConsole()
    config = load_config(args.config)
    orgs = merge_orgs(config.orgs, args.org, args.orgs_file)
    if not orgs:
        raise SystemExit("No organisations configured. Use --config, --org, or --orgs-file.")
    concurrency = capped_concurrency(args.concurrency if args.concurrency is not None else config.concurrency)
    auth = make_auth_provider(args.auth, args.tenant)
    return console, orgs, concurrency, auth, config


async def _doctor_async(args: argparse.Namespace) -> int:
    console, orgs, concurrency, auth, config = _load_runtime(args)
    async with AzureDevOpsHttpClient(auth, config.user_agent, concurrency) as client:
        await run_doctor(client, auth, orgs, console, args.source)
    return 0


def _doctor_command(args: argparse.Namespace) -> int:
    return asyncio.run(_doctor_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    console, orgs, concurrency, auth, config = _load_runtime(args)
    start = args.start or (datetime.now(timezone.utc) - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = args.end or datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)
    if end < start:
        raise SystemExit("--end must be on or after --start")
    sources = args.source
    combined = CollectionResult()
    async with AzureDevOpsHttpClient(auth, config.user_agent, concurrency) as client:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), TimeElapsedColumn(), console=console) as progress:
            task_id = progress.add_task("Collecting Azure DevOps build-minute data", total=None)
            if "analytics" in sources:
                progress.update(task_id, description="Collecting Analytics OData")
                combined.extend(await collect_analytics(client, orgs, start, end))
            if "jobrequests" in sources:
                progress.update(task_id, description="Collecting pool jobrequests")
                combined.extend(await collect_jobrequests(client, orgs, start, end, args.jobrequest_count))
            if "timeline" in sources:
                progress.update(task_id, description="Collecting build timelines (expensive/resumable)")
                combined.extend(await collect_timeline(client, orgs, start, end, args.state_file))
            if "billing" in sources:
                progress.update(task_id, description="Collecting unsupported billing cross-check")
                combined.extend(await collect_billing(client, orgs))
            progress.update(task_id, description="Writing outputs")
    written = write_outputs(
        combined,
        args.out_dir,
        args.format,
        start.date().isoformat(),
        end.date().isoformat(),
        rates=config.actions_cost_rates,
        include_actions_cost=args.actions_cost_model,
        expected_orgs=orgs,
        explicit_headline_source=sources[0] if len(sources) == 1 else None,
    )
    console.print(f"Wrote {len(written['written'])} file(s) to {args.out_dir}; primary source: {written['primary_source']}")
    if combined.failures:
        console.print(f"Completed with {len(combined.failures)} recoverable failure(s); see executive summary/summary.json.")
    return 0


def _run_command(args: argparse.Namespace) -> int:
    return asyncio.run(_run_async(args))


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = args.func(args)
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # noqa: BLE001 - top-level CLI should show concise failure.
        logging.getLogger(__name__).debug("Unhandled error", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
