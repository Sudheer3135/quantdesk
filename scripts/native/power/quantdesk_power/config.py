"""The power controller's configuration: one JSON file, validated completely before use.

Anything missing, of the wrong type, not absolute where a path is expected, or
outside its pattern is refused; the controller then does nothing. The Azure
identifiers are checked against strict patterns because they become path
components of the management URL.
"""
from __future__ import annotations

import json
import re
from datetime import time

SCHEMA = "quantdesk_power_config/1"
REQUEST_SCHEMA = "quantdesk_power_request_config/1"
REQUEST_KEYS = {"schema", "request_dir", "window"}
# Ubuntu's unattended-upgrades.service is the long-running shutdown helper, active
# whenever the machine is up; it is not an update in progress (apt-daily-upgrade.service
# is). Listing it would make every shutdown look blocked.
NOT_UPDATE_ACTIVITY = {"unattended-upgrades.service"}
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RESOURCE_GROUP = re.compile(r"^[A-Za-z0-9._()-]{1,90}$")
VM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
API_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}(-preview)?$")
UNIT = re.compile(r"^[A-Za-z0-9@_.:-]+\.(service|timer)$")
ACCOUNT = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
VERSION_TEXT = re.compile(r"^\d+\.\d+\.\d+$")
PROGRAMS = ("systemctl", "ps", "timedatectl", "nft", "psql", "pg_controldata")
TIMEOUTS = ("stop_web", "stop_api", "stop_redis", "stop_postgres", "start_unit",
            "db_statement", "evidence", "command")


class ConfigError(Exception):
    pass


def _abs(value, what: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or ".." in value.split("/"):
        raise ConfigError(f"{what} must be an absolute path without '..'")
    return value


def _clock(value, what: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{what} must be HH:MM") from exc
    return parsed


def _match(pattern: re.Pattern, value, what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ConfigError(f"{what} is not valid")
    return value


def _positive(value, what: str, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= upper:
        raise ConfigError(f"{what} must be a number in (0, {upper}]")
    return float(value)


def _window(cfg: dict) -> None:
    window = cfg.get("window")
    if not isinstance(window, dict):
        raise ConfigError("window is missing")
    days = window.get("weekdays")
    if not isinstance(days, list) or not days or not all(
            isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
        raise ConfigError("window.weekdays must list weekday numbers 0-6")
    first = _clock(window.get("first_attempt_ist"), "window.first_attempt_ist")
    last = _clock(window.get("last_attempt_ist"), "window.last_attempt_ist")
    if not first < last:
        raise ConfigError("the attempt window must end after it starts")


def validate_request(cfg) -> dict:
    """The unprivileged requester's configuration: the request directory and the window.

    Nothing else is accepted, so no database, Azure, observer or controller
    setting can be handed to the requester by mistake.
    """
    if not isinstance(cfg, dict) or cfg.get("schema") != REQUEST_SCHEMA:
        raise ConfigError(f"the requester's configuration is not {REQUEST_SCHEMA}")
    if set(cfg) != REQUEST_KEYS:
        raise ConfigError(f"the requester's configuration must hold exactly "
                          f"{', '.join(sorted(REQUEST_KEYS))}")
    _abs(cfg.get("request_dir"), "request_dir")
    _window(cfg)
    return cfg


def validate(cfg) -> dict:
    if not isinstance(cfg, dict) or cfg.get("schema") != SCHEMA:
        raise ConfigError(f"the configuration is not {SCHEMA}")
    for key in ("state_root", "request_dir", "calendar_file", "holds_dir"):
        _abs(cfg.get(key), key)
    _match(ACCOUNT, cfg.get("requester_user"), "requester_user")
    _window(cfg)
    obs = cfg.get("observer")
    if not isinstance(obs, dict):
        raise ConfigError("observer is missing")
    _match(ACCOUNT, obs.get("user"), "observer.user")
    for key in ("state_root", "python", "entry", "config"):
        _abs(obs.get(key), f"observer.{key}")
    versions = obs.get("accepted_versions")
    if not isinstance(versions, list) or not versions or not all(
            isinstance(v, str) and VERSION_TEXT.match(v) for v in versions):
        raise ConfigError("observer.accepted_versions must list N.N.N versions")
    timers = obs.get("timers")
    if not isinstance(timers, list) or not timers or not all(
            isinstance(t, str) and UNIT.match(t) and t.endswith(".timer") for t in timers):
        raise ConfigError("observer.timers must list timer units")
    _positive(obs.get("lock_wait_seconds"), "observer.lock_wait_seconds", 1800)
    _positive(obs.get("evidence_max_age_seconds"), "observer.evidence_max_age_seconds", 600)
    units = cfg.get("units")
    if not isinstance(units, dict) or set(units) != {"web", "api", "redis", "postgres"}:
        raise ConfigError("units must name web, api, redis and postgres")
    for key, unit in units.items():
        if not isinstance(unit, str) or not UNIT.match(unit) or not unit.endswith(".service"):
            raise ConfigError(f"units.{key} must be a service unit")
    if not isinstance(cfg.get("api_process_match"), str) or not cfg["api_process_match"]:
        raise ConfigError("api_process_match is missing")
    programs = cfg.get("programs")
    if not isinstance(programs, dict) or set(programs) != set(PROGRAMS):
        raise ConfigError(f"programs must name exactly {', '.join(PROGRAMS)}")
    for key in PROGRAMS:
        _abs(programs[key], f"programs.{key}")
    pg = cfg.get("postgres")
    if not isinstance(pg, dict):
        raise ConfigError("postgres is missing")
    for key in ("service_file", "pass_file", "data_dir"):
        _abs(pg.get(key), f"postgres.{key}")
    _match(ROLE, pg.get("service"), "postgres.service")
    _match(ACCOUNT, pg.get("cluster_user"), "postgres.cluster_user")
    _match(ROLE, pg.get("app_role"), "postgres.app_role")
    timeouts = cfg.get("timeouts")
    if not isinstance(timeouts, dict) or set(timeouts) != set(TIMEOUTS):
        raise ConfigError(f"timeouts must set exactly {', '.join(TIMEOUTS)}")
    for key in TIMEOUTS:
        _positive(timeouts[key], f"timeouts.{key}", 600)
    for key in ("package_locks",):
        items = cfg.get(key)
        if not isinstance(items, list) or not items:
            raise ConfigError(f"{key} must be a non-empty list")
        for item in items:
            _abs(item, key)
    apt_units = cfg.get("apt_units")
    if not isinstance(apt_units, list) or not apt_units or not all(
            isinstance(u, str) and UNIT.match(u) and u.endswith(".service") for u in apt_units):
        raise ConfigError("apt_units must list service units")
    if NOT_UPDATE_ACTIVITY & set(apt_units):
        raise ConfigError(f"apt_units must not list {', '.join(NOT_UPDATE_ACTIVITY)}: it is "
                          "active whenever the machine is up, not an update in progress")
    package_timers = cfg.get("package_timers")
    if not isinstance(package_timers, list) or not package_timers or not all(
            isinstance(t, str) and UNIT.match(t) and t.endswith(".timer")
            for t in package_timers):
        raise ConfigError("package_timers must list the automatic-update timer units")
    imds = cfg.get("imds")
    if not isinstance(imds, dict) or set(imds) != {"nft_table"} \
            or not isinstance(imds.get("nft_table"), str) \
            or not re.match(r"^inet [a-z_]+$", imds["nft_table"]):
        raise ConfigError("imds must name exactly the nft table (inet <name>)")
    status = cfg.get("status")
    if not isinstance(status, dict) or set(status) != {"key_file", "transport"}:
        raise ConfigError("status must set exactly key_file and transport")
    _abs(status.get("key_file"), "status.key_file")
    if status.get("transport") != "none":
        raise ConfigError("status.transport must be \"none\": publishing the status outside "
                          "this machine is not authorized")
    az = cfg.get("azure")
    if not isinstance(az, dict):
        raise ConfigError("azure is missing")
    _match(GUID, az.get("subscription_id"), "azure.subscription_id")
    _match(RESOURCE_GROUP, az.get("resource_group"), "azure.resource_group")
    _match(VM_NAME, az.get("vm_name"), "azure.vm_name")
    _match(API_VERSION, az.get("api_version"), "azure.api_version")
    _positive(az.get("request_timeout_seconds"), "azure.request_timeout_seconds", 30)
    _positive(az.get("poll_budget_seconds"), "azure.poll_budget_seconds", 3600)
    return cfg


def load_request(path: str) -> dict:
    return validate_request(_read(path))


def load(path: str) -> dict:
    return validate(_read(path))


def _read(path: str):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
    except OSError as exc:
        raise ConfigError(f"{path} cannot be read ({exc.strerror})") from exc
    if len(raw) > 1024 * 1024:
        raise ConfigError(f"{path} is too large")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{path} is not JSON") from exc
