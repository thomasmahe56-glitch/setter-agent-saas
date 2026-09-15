"""Pure follow-up scheduling rules. All timestamps are stored in UTC."""

from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo


UNITS = {"minutes": 60, "hours": 3600, "days": 86400}


def normalize_stages(raw: list[dict]) -> list[dict]:
    stages = []
    for index, item in enumerate(raw[:8], 1):
        if not isinstance(item, dict):
            continue
        unit = str(item.get("delay_unit") or "hours")
        value = item.get("delay_value", item.get("delay_hours"))
        mode = str(item.get("mode") or "manual")
        if unit not in UNITS or mode not in {"auto", "manual", "assisted"}:
            raise ValueError("Invalid follow-up unit or mode")
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid follow-up delay") from exc
        if not math.isfinite(value) or value <= 0 or value * UNITS[unit] > 365 * 86400:
            raise ValueError("Follow-up delay must be positive and bounded")
        stages.append({
            "stage": f"follow_up_{index}", "stage_index": index,
            "enabled": item.get("enabled") is True,
            "delay_value": value, "delay_unit": unit,
            "mode": "manual" if mode == "assisted" else mode,
            "ai_instruction": str(item.get("ai_instruction") or "")[:1000],
        })
    return stages


def _local_candidates(day, hour: int, minute: int, zone: ZoneInfo) -> list[datetime]:
    """Return real instants for the first valid opening wall minute.

    A spring-forward gap advances to the next valid minute. An autumn overlap
    may have two real instants for the same opening wall time.
    """
    naive = datetime(day.year, day.month, day.day, hour, minute)
    for offset in range(180):
        wall = naive + timedelta(minutes=offset)
        candidates = set()
        for fold in (0, 1):
            aware = wall.replace(tzinfo=zone, fold=fold)
            instant = aware.astimezone(timezone.utc)
            if instant.astimezone(zone).replace(tzinfo=None) == wall:
                candidates.add(instant)
        if candidates:
            return sorted(candidates)
    raise ValueError("No valid local opening instant")


def next_open_at(instant: datetime, start: str, end: str, tz_name: str) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("A timezone-aware instant is required")
    zone = ZoneInfo(tz_name)
    instant = instant.astimezone(timezone.utc)
    local = instant.astimezone(zone)
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    opening = sh * 60 + sm
    closing = eh * 60 + em
    minute = local.hour * 60 + local.minute
    if opening == closing or (opening < closing and opening <= minute < closing) or (
        opening > closing and (minute >= opening or minute < closing)
    ):
        return instant
    today = _local_candidates(local.date(), sh, sm, zone)
    candidate = next((opening_at for opening_at in today if opening_at > instant), None)
    if candidate is not None:
        return candidate
    return _local_candidates(local.date() + timedelta(days=1), sh, sm, zone)[0]


def schedule_stage(anchor: datetime, stage: dict, settings: dict) -> tuple[datetime, datetime]:
    delay = timedelta(seconds=stage["delay_value"] * UNITS[stage["delay_unit"]])
    due_at = anchor.astimezone(timezone.utc) + delay
    scheduled_at = next_open_at(
        due_at, settings["allowed_send_start"], settings["allowed_send_end"],
        settings["timezone"],
    )
    return due_at, scheduled_at
