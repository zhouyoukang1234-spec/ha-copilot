"""Helpers for the Lab Sim digital-twin layer.

Deterministic, dependency-free helpers that turn a bare ``entity_id`` into a
human-friendly name and a plausible initial state/unit for sensors — so the
twin mirrors the real Mijia home closely enough to exercise the user's real
automations, scripts, scenes and Lovelace.
"""

from __future__ import annotations

import hashlib

# A small curated pinyin -> Chinese map for the most visible devices, so the
# twin reads naturally in the UI. Anything not listed falls back to a title
# cased object_id.
_NAME_HINTS = {
    "yi_hao_deng": "一号灯",
    "er_hao_deng": "二号灯",
    "san_hao_deng": "三号灯",
    "si_hao_deng": "四号灯",
    "wu_hao_deng": "五号灯",
    "ding_deng_1": "顶灯1",
    "ding_deng_2": "顶灯2",
    "tong_deng_1": "筒灯1",
    "tong_deng_2": "筒灯2",
    "chuang_di_deng": "床底灯",
    "chuangmi": "创米摄像头",
    "ke_ting_deng": "客厅灯",
    "wo_shi_deng": "卧室灯",
}


def humanize(entity_id: str) -> str:
    """Best-effort friendly name from an entity_id."""
    obj = entity_id.split(".", 1)[1]
    for key, label in _NAME_HINTS.items():
        if key in obj:
            return label
    return obj.replace("_", " ").title()


def _seed(entity_id: str) -> int:
    """Stable per-entity seed so values are deterministic across restarts."""
    return int(hashlib.md5(entity_id.encode()).hexdigest()[:8], 16)


def _span(entity_id: str, lo: float, hi: float, ndigits: int = 1) -> float:
    frac = (_seed(entity_id) % 1000) / 1000.0
    val = lo + (hi - lo) * frac
    return round(val, ndigits) if ndigits else round(val)


def guess_sensor(entity_id: str) -> tuple[str, str | None, str | None]:
    """Return (state, unit_of_measurement, device_class) for a mock sensor."""
    n = entity_id.lower()

    def num(lo, hi, nd=1):
        return str(_span(entity_id, lo, hi, nd))

    if "temperature" in n:
        return num(18, 27), "°C", "temperature"
    if "humidity" in n:
        return num(40, 65, 0), "%", "humidity"
    if "battery_level" in n or n.endswith("_battery") or "battery_percent" in n:
        return num(35, 100, 0), "%", "battery"
    if "out_power" in n or "in_power" in n or "_power" in n:
        return num(0, 600), "W", "power"
    if "energy" in n:
        return num(0, 50, 2), "kWh", "energy"
    if "volts" in n or "voltage" in n:
        return num(3, 240), "V", "voltage"
    if "current" in n:
        return num(0, 12, 2), "A", "current"
    if "remaining_time" in n or "remain_time" in n:
        return num(0, 600, 0), "min", "duration"
    if "capacity" in n:
        return num(500, 5000, 0), "mAh", None
    if "cycles" in n:
        return num(0, 500, 0), None, None
    if "free_space" in n or "storage" in n:
        return num(1, 64, 0), "GB", "data_size"
    if "state_of_health" in n:
        return num(80, 100, 0), "%", None
    if "shi_chang" in n:  # 时长 (duration text)
        return "08:30", None, None
    if "shi_jian" in n:  # 时间 (time text)
        return "07:00", None, None
    if "status" in n or n.endswith("_state") or "charging_state" in n:
        return "正常", None, None
    if "print_status" in n:
        return "idle", None, None
    return num(0, 100), None, None
