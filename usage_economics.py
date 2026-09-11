from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


COST_ACCURACIES = {
    "provider_reported",
    "provider_usage_priced",
    "estimated",
    "unknown",
}


def decimal_value(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def month_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def cost_value(row: dict[str, Any]) -> Decimal | None:
    accuracy = str(row.get("cost_accuracy") or "unknown")
    value = decimal_value(row.get("cost_eur"))
    if accuracy == "unknown" or value is None:
        return None
    return value


def _rounded(value: Decimal, places: int = 8) -> float:
    return float(round(value, places))


def aggregate_costs(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    total = Decimal("0")
    known_operations = 0
    unknown_operations = 0
    by_accuracy: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_module: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_feature: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_provider: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_model: defaultdict[str, Decimal] = defaultdict(Decimal)
    tokens = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "reasoning": 0}
    api_runs = 0
    scraping_runs = 0

    materialized = list(rows)
    for row in materialized:
        accuracy = str(row.get("cost_accuracy") or "unknown")
        if accuracy not in COST_ACCURACIES:
            accuracy = "unknown"
        value = cost_value(row)
        if value is None:
            unknown_operations += 1
        else:
            known_operations += 1
            total += value
            by_accuracy[accuracy] += value
            by_module[str(row.get("module") or "unknown")] += value
            by_feature[str(row.get("feature") or "unknown")] += value
            by_provider[str(row.get("provider") or "unknown")] += value
            by_model[str(row.get("model") or "unknown")] += value

        tokens["input"] += int(row.get("input_tokens") or 0)
        tokens["output"] += int(row.get("output_tokens") or 0)
        tokens["cache_creation"] += int(row.get("cache_creation_input_tokens") or 0)
        tokens["cache_read"] += int(row.get("cache_read_input_tokens") or 0)
        tokens["reasoning"] += int(row.get("reasoning_tokens") or 0)
        if str(row.get("unit") or "") in {"run", "api_call", "generation"}:
            api_runs += 1
        if row.get("module") == "prospecting" and row.get("provider") == "apify":
            scraping_runs += 1

    operation_count = known_operations + unknown_operations
    coverage = (known_operations / operation_count * 100) if operation_count else None
    unknown_share = (unknown_operations / operation_count * 100) if operation_count else None

    def money_map(values: dict[str, Decimal]) -> dict[str, float]:
        return {key: _rounded(value) for key, value in sorted(values.items())}

    accuracy_costs = money_map(by_accuracy)

    def cost_share(accuracy: str) -> float | None:
        if total <= 0:
            return None
        return round(float(by_accuracy[accuracy] / total * 100), 2)

    return {
        "total_cost_eur": _rounded(total),
        "tracked_cost_eur": _rounded(total),
        "known_operations": known_operations,
        "untracked_operations": unknown_operations,
        "tracking_coverage_percent": round(coverage, 2) if coverage is not None else None,
        "cost_by_accuracy": accuracy_costs,
        "provider_reported_share_percent": cost_share("provider_reported"),
        "provider_usage_priced_share_percent": cost_share("provider_usage_priced"),
        "estimated_share_percent": cost_share("estimated"),
        "unknown_operation_share_percent": round(unknown_share, 2) if unknown_share is not None else None,
        "cost_by_module": money_map(by_module),
        "cost_by_feature": money_map(by_feature),
        "cost_by_provider": money_map(by_provider),
        "cost_by_model": money_map(by_model),
        "tokens": tokens,
        "api_runs": api_runs,
        "scraping_runs": scraping_runs,
    }


def safe_unit_cost(total_cost: float | Decimal, denominator: int | float | Decimal | None) -> float | None:
    denominator_value = decimal_value(denominator)
    if denominator_value is None or denominator_value <= 0:
        return None
    cost = decimal_value(total_cost)
    if cost is None:
        return None
    return _rounded(cost / denominator_value)


def usage_quantity(rows: Iterable[dict[str, Any]], *, module: str | None = None, event_type: str | None = None) -> int:
    total = Decimal("0")
    for row in rows:
        if module is not None and row.get("module") != module:
            continue
        if event_type is not None and row.get("event_type") != event_type:
            continue
        total += decimal_value(row.get("quantity")) or Decimal("0")
    return int(total)


ACTIVITY_LABELS = {
    "discovery": "Discovery",
    "assistant_message": "Message Angellos",
    "follow_up": "Relance",
    "dm_generation": "DM de prospection",
    "prospect_analysis": "Analyse de prospect",
    "training": "Action de training",
}


def _activity_kind(rows: list[dict[str, Any]]) -> tuple[str, str]:
    event_types = {str(row.get("event_type") or "") for row in rows}
    modules = {str(row.get("module") or "") for row in rows}
    if "source_discovery" in event_types:
        return "discovery", ACTIVITY_LABELS["discovery"]
    if "assistant_reply_generated" in event_types:
        return "assistant_message", ACTIVITY_LABELS["assistant_message"]
    if "follow_up_generated" in event_types:
        return "follow_up", ACTIVITY_LABELS["follow_up"]
    if "dm_generated" in event_types:
        return "dm_generation", ACTIVITY_LABELS["dm_generation"]
    if "prospect_analyzed" in event_types or "prospect_qualified" in event_types:
        return "prospect_analysis", ACTIVITY_LABELS["prospect_analysis"]
    if "training" in modules:
        return "training", ACTIVITY_LABELS["training"]
    first = rows[0]
    raw_kind = str(first.get("feature") or first.get("event_type") or first.get("module") or "other")
    return raw_kind, raw_kind.replace("_", " ").strip().capitalize()


def _activity_timestamp(row: dict[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(row.get("occurred_at") or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def build_cost_activities(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group immutable ledger rows into admin-facing business activities."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for position, row in enumerate(rows):
        user_id = str(row.get("user_id") or "unknown")
        run_id = row.get("run_id")
        if run_id:
            group_key = f"{user_id}:run:{run_id}"
        else:
            stable_id = (
                row.get("provider_event_id")
                or row.get("idempotency_key")
                or row.get("id")
                or f"row-{position}"
            )
            group_key = f"{user_id}:event:{stable_id}"
        groups.setdefault(group_key, []).append(row)

    activities: list[dict[str, Any]] = []
    for group_key, group_rows in groups.items():
        ordered = sorted(group_rows, key=_activity_timestamp)
        kind, label = _activity_kind(ordered)
        known_cost = sum((cost_value(row) or Decimal("0") for row in ordered), Decimal("0"))
        unknown_operations = sum(1 for row in ordered if cost_value(row) is None)
        quantities = sum((decimal_value(row.get("quantity")) or Decimal("0") for row in ordered), Decimal("0"))
        tokens = {
            "input": sum(int(row.get("input_tokens") or 0) for row in ordered),
            "output": sum(int(row.get("output_tokens") or 0) for row in ordered),
            "cache_creation": sum(int(row.get("cache_creation_input_tokens") or 0) for row in ordered),
            "cache_read": sum(int(row.get("cache_read_input_tokens") or 0) for row in ordered),
            "reasoning": sum(int(row.get("reasoning_tokens") or 0) for row in ordered),
        }
        first = ordered[0]
        last = ordered[-1]
        activities.append({
            "id": group_key,
            "activity_type": kind,
            "label": label,
            "module": str(first.get("module") or "unknown"),
            "user_id": str(first.get("user_id") or "unknown"),
            "run_id": first.get("run_id"),
            "campaign_id": next((row.get("campaign_id") for row in ordered if row.get("campaign_id")), None),
            "conversation_id": next((row.get("conversation_id") for row in ordered if row.get("conversation_id")), None),
            "started_at": first.get("occurred_at"),
            "ended_at": last.get("occurred_at"),
            "total_cost_eur": _rounded(known_cost),
            "cost_complete": unknown_operations == 0,
            "unknown_operations": unknown_operations,
            "known_operations": len(ordered) - unknown_operations,
            "event_count": len(ordered),
            "quantity": _rounded(quantities, 6),
            "providers": sorted({str(row.get("provider")) for row in ordered if row.get("provider")}),
            "models": sorted({str(row.get("model")) for row in ordered if row.get("model")}),
            "features": sorted({str(row.get("feature")) for row in ordered if row.get("feature")}),
            "event_types": sorted({str(row.get("event_type")) for row in ordered if row.get("event_type")}),
            "tokens": tokens,
        })
    return sorted(activities, key=lambda item: _activity_timestamp({"occurred_at": item.get("started_at")}), reverse=True)


def summarize_cost_activities(activities: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        grouped.setdefault(str(activity.get("activity_type") or "other"), []).append(activity)

    summaries: dict[str, dict[str, Any]] = {}
    for activity_type, items in grouped.items():
        costs = [decimal_value(item.get("total_cost_eur")) or Decimal("0") for item in items]
        total = sum(costs, Decimal("0"))
        complete_count = sum(1 for item in items if item.get("cost_complete"))
        summaries[activity_type] = {
            "activity_type": activity_type,
            "label": str(items[0].get("label") or activity_type),
            "activity_count": len(items),
            "total_known_cost_eur": _rounded(total),
            "average_known_cost_eur": _rounded(total / Decimal(len(items))) if items else None,
            "minimum_known_cost_eur": _rounded(min(costs)) if costs else None,
            "maximum_known_cost_eur": _rounded(max(costs)) if costs else None,
            "complete_activities": complete_count,
            "incomplete_activities": len(items) - complete_count,
            "coverage_percent": round(complete_count / len(items) * 100, 2) if items else None,
        }
    return dict(sorted(summaries.items(), key=lambda item: (-item[1]["total_known_cost_eur"], item[0])))


def credit_summary(
    transactions: Iterable[dict[str, Any]],
    credits_enabled: bool,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> dict[str, Any]:
    if not credits_enabled:
        return {
            "credits_enabled": False,
            "credits_used": None,
            "credits_remaining": None,
            "credits_included": None,
        }
    balance = Decimal("0")
    used = Decimal("0")
    included = Decimal("0")
    for row in transactions:
        credits = decimal_value(row.get("credits")) or Decimal("0")
        balance += credits
        in_period = True
        if period_start is not None and period_end is not None:
            try:
                occurred = datetime.fromisoformat(str(row.get("occurred_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
                in_period = period_start <= occurred < period_end
            except (TypeError, ValueError):
                in_period = False
        if in_period and row.get("transaction_type") == "usage_debit":
            used += abs(credits)
        if in_period and row.get("transaction_type") == "monthly_grant":
            included += credits
    return {
        "credits_enabled": True,
        "credits_used": _rounded(used, 6),
        "credits_remaining": _rounded(balance, 6),
        "credits_included": _rounded(included, 6),
    }


def select_credit_rule(
    rules: Iterable[dict[str, Any]],
    usage_event: dict[str, Any],
    occurred_at: datetime,
) -> dict[str, Any] | None:
    """Resolve the newest enabled rule valid when the immutable usage occurred."""
    valid: list[tuple[datetime, dict[str, Any]]] = []
    instant = occurred_at.astimezone(timezone.utc)
    for rule in rules:
        if not rule.get("enabled"):
            continue
        if rule.get("module") != usage_event.get("module") or rule.get("event_type") != usage_event.get("event_type"):
            continue
        try:
            start = datetime.fromisoformat(str(rule.get("effective_from")).replace("Z", "+00:00")).astimezone(timezone.utc)
            end_raw = rule.get("effective_to")
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")).astimezone(timezone.utc) if end_raw else None
        except (TypeError, ValueError):
            continue
        if start <= instant and (end is None or instant < end):
            valid.append((start, rule))
    return max(valid, key=lambda item: item[0])[1] if valid else None


def build_credit_debit(usage_event: dict[str, Any], rule: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build an idempotent debit; no active rule means credits remain disabled."""
    if rule is None:
        return None
    quantity = decimal_value(usage_event.get("quantity")) or Decimal("0")
    per_unit = decimal_value(rule.get("credits_per_unit"))
    usage_id = usage_event.get("id")
    user_id = usage_event.get("user_id")
    if not usage_id or not user_id or per_unit is None or per_unit < 0:
        return None
    debit = -(quantity * per_unit)
    if debit == 0:
        return None
    return {
        "user_id": user_id,
        "usage_ledger_id": usage_id,
        "transaction_type": "usage_debit",
        "credits": _rounded(debit, 6),
        "rule_version": rule.get("version"),
        "idempotency_key": f"usage_debit:{usage_id}:{rule.get('version')}",
        "metadata": {"credit_rule_id": rule.get("id")},
    }
