"""炭窑焖烧志业务规则。"""

from __future__ import annotations

from charclamp.domain.models import BurnShift, Clamp

MIN_PEAK_TEMP_FOR_DRAWN = 400.0


class RuleError(ValueError):
    """业务规则校验失败。"""


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    return max(clamp.shifts, key=lambda s: s.started_at)


def can_mark_clamp_drawn(clamp: Clamp) -> tuple[bool, str]:
    """
    炭窑转为「已出炭」(drawn) 的前提：
    最近一条焖烧班次的峰值温度已记录，且 >= 400℃。
    """
    latest = latest_shift_for_clamp(clamp)
    if latest is None:
        return False, "该窑尚无焖烧班次，不能标记为已出炭"
    if latest.peak_temp_c is None:
        return False, "最近班次尚未记录峰值温度，不能标记为已出炭"
    if latest.peak_temp_c < MIN_PEAK_TEMP_FOR_DRAWN:
        return (
            False,
            f"最近班次峰值温度 {latest.peak_temp_c}℃ 低于 {MIN_PEAK_TEMP_FOR_DRAWN:.0f}℃，不能标记为已出炭",
        )
    return True, ""


def assert_can_set_clamp_status(clamp: Clamp, new_status: str) -> None:
    allowed = {Clamp.STATUS_STACKED, Clamp.STATUS_BURNING, Clamp.STATUS_DRAWN}
    if new_status not in allowed:
        raise RuleError(f"无效状态：{new_status}")
    if new_status == Clamp.STATUS_DRAWN:
        ok, msg = can_mark_clamp_drawn(clamp)
        if not ok:
            raise RuleError(msg)


def assert_can_update_peak(shift: BurnShift, clamp: Clamp, new_peak: float | None) -> None:
    """
    修改班次峰值温度的规则：
    - 窑已出炭：峰值锁死，禁止任何修改；
    - 焖烧中已填峰值：只允许改大，禁止清空或改小；
    - 尚未填峰值：允许首次记录。
    """
    if clamp.status == Clamp.STATUS_DRAWN:
        raise RuleError(f"窑 {clamp.code} 已出炭，峰值温度已锁死，禁止修改")
    current = shift.peak_temp_c
    if current is None:
        return
    if new_peak is None:
        raise RuleError(f"焖烧中已填峰值（当前 {current:.0f}℃）禁止清空，只允许改大")
    if new_peak <= current:
        raise RuleError(
            f"焖烧中已填峰值只允许改大：当前 {current:.0f}℃，不允许改为 {new_peak:.0f}℃"
        )
