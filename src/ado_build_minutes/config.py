"""Configuration loading for ado-build-minutes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


@dataclass
class AppConfig:
    """Configuration values loaded from TOML and CLI overrides."""

    orgs: list[str] = field(default_factory=list)
    concurrency: int = 6
    user_agent: str = "ado-build-minutes/0.1 (+https://github.com/vicperdana/ado-build-minutes)"
    actions_cost_rates: dict[str, float] = field(default_factory=dict)


def load_config(path: str | None) -> AppConfig:
    """Load configuration from TOML, returning defaults when no file exists."""
    if path is None:
        default = Path("config.toml")
        if not default.exists():
            return AppConfig()
        path = str(default)
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {cfg_path}")
    with cfg_path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    rates = {str(k): float(v) for k, v in (raw.get("actions_cost_rates") or {}).items() if isinstance(v, int | float)}
    return AppConfig(
        orgs=[str(org) for org in raw.get("orgs", [])],
        concurrency=int(raw.get("concurrency", 6)),
        user_agent=str(raw.get("user_agent") or AppConfig.user_agent),
        actions_cost_rates=rates,
    )


def merge_orgs(config_orgs: list[str], cli_orgs: list[str], orgs_file: str | None) -> list[str]:
    """Merge org names from config, repeatable CLI flags, and an optional file."""
    orgs = list(config_orgs) + list(cli_orgs)
    if orgs_file:
        with open(orgs_file, "r", encoding="utf-8") as handle:
            orgs.extend(line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#"))
    seen: set[str] = set()
    merged: list[str] = []
    for org in orgs:
        key = org.casefold()
        if key not in seen:
            seen.add(key)
            merged.append(org)
    return merged


def capped_concurrency(value: int) -> int:
    """Return a conservative Azure DevOps concurrency value capped at 20."""
    return max(1, min(int(value), 20))
