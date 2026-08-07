"""Configuration loading.

Secrets come from environment variables (loaded from a local ``.env`` if present
via python-dotenv, or injected by CI). Non-secret settings and the location list
come from ``config.yaml``. Locations may also be supplied purely via the
``TOAST_RESTAURANT_GUIDS`` env var (handy for CI) — in that case they get
auto-generated names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import Location


@dataclass
class ToastCredentials:
    host: str
    client_id: str
    client_secret: str
    user_access_type: str = "TOAST_MACHINE_CLIENT"

    @property
    def is_complete(self) -> bool:
        return bool(self.host and self.client_id and self.client_secret)


@dataclass
class ReportSettings:
    week_start: str = "monday"  # monday | sunday
    output_dir: str = "output"
    title: str = "Weekly Labor & Sales Report"
    # Job titles (case-insensitive) whose time entries are excluded from ALL
    # labor figures — e.g. "Register", which is a POS role, not real labor.
    exclude_roles: list[str] = field(default_factory=lambda: ["Register"])
    # Ordered role buckets for the "Labor hours by role" board: bucket -> the
    # Toast job titles that roll up into it. Titles not listed get their own row.
    role_groups: dict = field(
        default_factory=lambda: {
            "Kitchen": ["Kitchen"],
            "Staff": ["Staff"],
            "Shift Capt/Lead": ["Shift Capt", "Shift Lead"],
            # General Manager is excluded from labor entirely (see exclude_roles),
            # so this bucket is Chef only.
            "Chef": ["Chef"],
        }
    )


@dataclass
class AppConfig:
    credentials: ToastCredentials
    locations: list[Location] = field(default_factory=list)
    report: ReportSettings = field(default_factory=ReportSettings)


def load_config(config_path: str | os.PathLike[str] | None = "config.yaml") -> AppConfig:
    """Load credentials from the environment and settings/locations from YAML."""
    load_dotenv()  # no-op if .env is absent

    def _env(name: str, default: str = "") -> str:
        # Strip whitespace: pasting into a secrets UI often adds a trailing
        # newline/space, which silently breaks auth. Empty -> default.
        value = (os.getenv(name) or "").strip()
        return value or default

    credentials = ToastCredentials(
        host=_env("TOAST_API_HOST", "https://ws-api.toasttab.com").rstrip("/"),
        client_id=_env("TOAST_CLIENT_ID"),
        client_secret=_env("TOAST_CLIENT_SECRET"),
        user_access_type=_env("TOAST_USER_ACCESS_TYPE", "TOAST_MACHINE_CLIENT"),
    )

    report = ReportSettings()
    locations: list[Location] = []

    path = Path(config_path) if config_path else None
    if path and path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        for loc in raw.get("locations", []) or []:
            locations.append(
                Location(
                    guid=str(loc["guid"]),
                    name=str(loc.get("name", loc["guid"])),
                    timezone=str(loc.get("timezone", "America/New_York")),
                )
            )
        r = raw.get("report", {}) or {}
        report = ReportSettings(
            week_start=str(r.get("week_start", report.week_start)).lower(),
            output_dir=str(r.get("output_dir", report.output_dir)),
            title=str(r.get("title", report.title)),
            exclude_roles=[str(x) for x in (r.get("exclude_roles") or report.exclude_roles)],
            role_groups=dict(r.get("role_groups") or report.role_groups),
        )

    # Env-var location list overrides/augments if provided. Each comma-separated
    # entry is a GUID, optionally followed by a friendly name after a colon:
    #   TOAST_RESTAURANT_GUIDS="guid-a:Pacific Beach,guid-b:Encinitas"
    # A name given here is authoritative (it won't be replaced by Toast's own
    # restaurant name, which is often the street address).
    env_guids = os.getenv("TOAST_RESTAURANT_GUIDS", "").strip()
    if env_guids:
        known = {loc.guid for loc in locations}
        for i, entry in enumerate(e.strip() for e in env_guids.split(",") if e.strip()):
            guid, _sep, raw_name = entry.partition(":")
            guid, raw_name = guid.strip(), raw_name.strip()
            if guid and guid not in known:
                locations.append(Location(guid=guid, name=raw_name or f"Location {i + 1}"))
                known.add(guid)

    return AppConfig(credentials=credentials, locations=locations, report=report)
