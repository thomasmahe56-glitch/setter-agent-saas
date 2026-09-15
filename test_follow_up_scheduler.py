from datetime import datetime, timedelta, timezone

from follow_up_scheduler import next_open_at, normalize_stages, schedule_stage


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_custom_delay_with_open_messaging():
    stage = normalize_stages([{"delay_value": 7, "delay_unit": "hours", "mode": "auto"}])[0]
    due, scheduled = schedule_stage(at(2026, 9, 15, 8), stage, {
        "allowed_send_start": "08:00", "allowed_send_end": "20:00", "timezone": "Europe/Paris"})
    assert due == at(2026, 9, 15, 15)
    assert scheduled == due  # 17:00 Paris


def test_closed_messaging_defers_but_preserves_theoretical_due_at():
    stage = normalize_stages([{"delay_value": 7, "delay_unit": "hours", "mode": "auto"}])[0]
    due, scheduled = schedule_stage(at(2026, 9, 15, 15), stage, {
        "allowed_send_start": "08:00", "allowed_send_end": "20:00", "timezone": "Europe/Paris"})
    assert due == at(2026, 9, 15, 22)  # Midnight in Paris
    assert scheduled == at(2026, 9, 16, 6)  # 08:00 in Paris


def test_virtual_clock_never_fires_early():
    stage = normalize_stages([{"delay_value": 7, "delay_unit": "hours", "mode": "auto"}])[0]
    t0 = at(2026, 9, 15, 8)
    due, _ = schedule_stage(t0, stage, {
        "allowed_send_start": "00:00", "allowed_send_end": "00:00", "timezone": "Europe/Paris"})
    assert t0 + timedelta(hours=6, minutes=59) < due
    assert t0 + timedelta(hours=7) == due
    assert t0 + timedelta(hours=8) > due


def test_paris_timezone_and_dst():
    settings = {"allowed_send_start": "08:00", "allowed_send_end": "20:00", "timezone": "Europe/Paris"}
    assert next_open_at(at(2026, 1, 15, 1), **{
        "start": settings["allowed_send_start"], "end": settings["allowed_send_end"], "tz_name": settings["timezone"]}) == at(2026, 1, 15, 7)
    assert next_open_at(at(2026, 7, 15, 1), **{
        "start": settings["allowed_send_start"], "end": settings["allowed_send_end"], "tz_name": settings["timezone"]}) == at(2026, 7, 15, 6)
    assert next_open_at(at(2026, 3, 29, 1), "08:00", "20:00", "Europe/Paris") == at(2026, 3, 29, 6)
    assert next_open_at(at(2026, 10, 25, 1), "08:00", "20:00", "Europe/Paris") == at(2026, 10, 25, 7)


def test_autumn_overlap_uses_the_next_occurrence_of_opening_wall_time():
    assert next_open_at(at(2026, 10, 25, 0, 15), "02:30", "03:30", "Europe/Paris") == at(2026, 10, 25, 0, 30)
    assert next_open_at(at(2026, 10, 25, 1, 15), "02:30", "03:30", "Europe/Paris") == at(2026, 10, 25, 1, 30)


def test_spring_gap_advances_nonexistent_opening_to_first_real_minute():
    assert next_open_at(at(2026, 3, 29, 0, 30), "02:30", "04:00", "Europe/Paris") == at(2026, 3, 29, 1)


def test_generic_stages_preserve_order_and_enabled_state():
    stages = normalize_stages([
        {"delay_value": 12, "delay_unit": "hours", "mode": "manual", "enabled": False},
        {"delay_value": 7, "delay_unit": "hours", "mode": "auto", "enabled": True},
    ])
    assert [(s["stage"], s["enabled"], s["delay_value"]) for s in stages] == [
        ("follow_up_1", False, 12), ("follow_up_2", True, 7)]
