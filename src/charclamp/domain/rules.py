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


def assert_can_update_peak(
    clamp_status: str,
    current_peak: float | None,
    new_peak: float | None,
    expected_peak: float | None,
    enforce_expected: bool = True,
) -> None:
    """
    峰值温度修改规则：
    - 窑已出炭：峰值锁死，禁止任何修改；
    - 并发只许一版生效：提交时的期望值必须等于库中当前值，否则视为冲突；
    - 焖烧中已填峰值：只允许改大，禁止清空或改小；
    - 未填峰值：允许首次登记。
    """
    if clamp_status == Clamp.STATUS_DRAWN:
        raise RuleError("该窑已出炭，峰值温度已锁死，禁止修改")
    if enforce_expected and expected_peak != current_peak:
        if current_peak is None:
            raise RuleError("峰值温度刚被他人填写，请刷新页面后重试")
        raise RuleError(f"峰值温度刚被他人更新为 {current_peak:.0f}℃，请刷新页面后重试")
    if current_peak is None:
        if new_peak is None:
            raise RuleError("尚未登记峰值温度，请先填写")
        return
    if new_peak is None:
        raise RuleError("已填峰值禁止清空")
    if new_peak <= current_peak:
        raise RuleError(f"已填峰值只允许改大（当前 {current_peak:.0f}℃），禁止改小或保持不变")
