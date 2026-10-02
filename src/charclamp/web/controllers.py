from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import or_, select, update
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, User
from charclamp.domain.rules import (
    RuleError,
    assert_can_set_clamp_status,
    assert_can_update_peak,
    can_mark_clamp_drawn,
)
from charclamp.infra.db import SessionLocal
from charclamp.infra.security import verify_password

STATUS_LABELS = {
    Clamp.STATUS_STACKED: "已码窑",
    Clamp.STATUS_BURNING: "焖烧中",
    Clamp.STATUS_DRAWN: "已出炭",
}


def _set_flash(request: Request, message: str, category: str = "ok") -> None:
    data = dict(request.session or {})
    data["flash"] = message
    data["flash_cat"] = category
    request.set_session(data)


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    data = dict(request.session or {})
    message = data.pop("flash", None)
    category = data.pop("flash_cat", None)
    if message is not None or category is not None:
        request.set_session(data)
    return message, category


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_optional_float(raw: Any) -> float | None:
    """解析可空浮点输入；空串/None -> None，非法数字抛 ValueError。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"非有限数值：{text}")
    return value


async def _load_timeline_context(clamp_id: int | None = None) -> dict[str, Any]:
    async with SessionLocal() as db:
        clamps = list(
            (
                await db.execute(
                    select(Clamp)
                    .options(selectinload(Clamp.site), selectinload(Clamp.shifts))
                    .order_by(Clamp.code)
                )
            )
            .scalars()
            .all()
        )
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
    }


class AuthController(Controller):
    path = ""
    tags = ["auth"]

    @get("/login", media_type=MediaType.HTML)
    async def login_page(self, request: Request) -> Template:
        flash, flash_cat = _pop_flash(request)
        return Template(
            template_name="login.html",
            context={"flash": flash, "flash_cat": flash_cat},
        )

    @post("/login")
    async def login(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user or not verify_password(password, user.password_hash):
                request.set_session({"flash": "用户名或密码错误", "flash_cat": "error"})
                return Redirect("/login")
            request.set_session({"user_id": user.id})
        return Redirect("/")

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.clear_session()
        return Redirect("/login")


class TimelineController(Controller):
    path = ""
    tags = ["timeline"]

    @get("/", media_type=MediaType.HTML)
    async def timeline(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="timeline.html",
            context={
                **ctx,
                "user": request.user,
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @get("/timeline/partial", media_type=MediaType.HTML)
    async def timeline_partial(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="partials/board.html",
            context={
                **ctx,
                "user": request.user,
            },
        )

    @get("/drawer/shift-new", media_type=MediaType.HTML)
    async def drawer_shift_new(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        async with SessionLocal() as db:
            clamps = list((await db.execute(select(Clamp).order_by(Clamp.code))).scalars().all())
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "user": request.user,
            },
        )

    @get("/drawer/clamp/{clamp_id:int}", media_type=MediaType.HTML)
    async def drawer_clamp(self, request: Request, clamp_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts), selectinload(Clamp.site))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
        can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "user": request.user,
            },
        )


class ShiftController(Controller):
    path = "/shifts"
    tags = ["shifts"]

    @post("/new")
    async def create_shift(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        started_raw = data.get("started_at") or ""
        started_at = datetime.fromisoformat(started_raw) if started_raw else datetime.utcnow()
        peak_raw = (data.get("peak_temp_c") or "").strip()
        peak = float(peak_raw) if peak_raw else None
        clamp_id = int(data["clamp_id"])
        async with SessionLocal() as db:
            shift = BurnShift(
                clamp_id=clamp_id,
                started_at=started_at,
                peak_temp_c=peak,
                charcoal_grade=(data.get("charcoal_grade") or "B").strip(),
                notes=(data.get("notes") or "").strip(),
            )
            db.add(shift)
            clamp = (
                await db.execute(select(Clamp).where(Clamp.id == clamp_id))
            ).scalar_one_or_none()
            if clamp and clamp.status == Clamp.STATUS_STACKED:
                clamp.status = Clamp.STATUS_BURNING
            await db.commit()
        _set_flash(request, "焖烧班次已登记", "ok")
        return Redirect(f"/?clamp_id={clamp_id}")

    @post("/{shift_id:int}/peak")
    async def set_peak(
        self,
        request: Request,
        shift_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        try:
            new_peak = _parse_optional_float(data.get("peak_temp_c"))
        except (TypeError, ValueError):
            _set_flash(request, "峰值温度必须是有效数字", "error")
            return Redirect("/")
        # 表单隐藏域 expected_peak 是乐观并发令牌：提交时的期望值必须等于库中现值，
        # 两人同时改同一班次时只许一版生效。缺失该字段的裸请求退化为仅校验单调递增。
        enforce_expected = "expected_peak" in data
        try:
            expected_peak = _parse_optional_float(data.get("expected_peak"))
        except (TypeError, ValueError):
            _set_flash(request, "峰值校验令牌无效，请刷新页面后重试", "error")
            return Redirect("/")

        # 第一段会话：读取现值做规则校验，给出准确的中文提示。
        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(BurnShift, Clamp)
                    .join(Clamp, BurnShift.clamp_id == Clamp.id)
                    .where(BurnShift.id == shift_id)
                )
            ).first()
            if row is None:
                _set_flash(request, "焖烧班次不存在", "error")
                return Redirect("/")
            shift, clamp = row
            clamp_id = clamp.id
            try:
                assert_can_update_peak(
                    clamp.status,
                    shift.peak_temp_c,
                    new_peak,
                    expected_peak,
                    enforce_expected=enforce_expected,
                )
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
                return Redirect(f"/?clamp_id={clamp_id}")

        # 第二段会话：原子守卫更新。期望值匹配 + 窑未出炭 + 单调递增全部写进
        # 同一条 UPDATE 的 WHERE，由数据库在单行上原子判定——并发请求只许一版
        # 生效，不存在“校验通过后被另一请求抢先”的 TOCTOU 窗口。
        conditions = [
            BurnShift.id == shift_id,
            or_(BurnShift.peak_temp_c.is_(None), BurnShift.peak_temp_c < new_peak),
            BurnShift.clamp_id.in_(
                select(Clamp.id).where(Clamp.status != Clamp.STATUS_DRAWN)
            ),
        ]
        if enforce_expected:
            if expected_peak is None:
                conditions.append(BurnShift.peak_temp_c.is_(None))
            else:
                conditions.append(BurnShift.peak_temp_c == expected_peak)
        stmt = (
            update(BurnShift)
            .where(*conditions)
            .values(peak_temp_c=new_peak)
            .execution_options(synchronize_session=False)
        )
        async with SessionLocal() as db:
            result = await db.execute(stmt)
            if result.rowcount == 1:
                await db.commit()
                _set_flash(request, f"峰值温度已更新为 {new_peak:.0f}℃", "ok")
            else:
                await db.rollback()
                _set_flash(request, "峰值温度刚被他人更新，请刷新页面后重试", "error")
        return Redirect(f"/?clamp_id={clamp_id}")


class ClampController(Controller):
    path = "/clamps"
    tags = ["clamps"]

    @post("/{clamp_id:int}/status")
    async def set_status(
        self,
        request: Request,
        clamp_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        new_status = (data.get("status") or "").strip()
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts))
                .with_for_update()
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            try:
                assert_can_set_clamp_status(clamp, new_status)
                clamp.status = new_status
                await db.commit()
                _set_flash(request, f"窑 {clamp.code} 状态已更新", "ok")
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")
