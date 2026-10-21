"""Confirmed operations on the account's current reservations."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import library
from .accounts import Account
from .control_api import _parse_moment
from .dependencies import current_account

router = APIRouter()


class TempLeaveRequest(BaseModel):
    auth_session_id: str
    uuid: str = Field(min_length=1)
    page: int = Field(default=1, ge=1, le=1_000_000)
    confirm: bool = False


def _owns_record(row, user_info):
    if row.get("appAccNo") is not None and user_info.get("accNo") is not None:
        return str(row["appAccNo"]) == str(user_info["accNo"])
    if row.get("logonName") and user_info.get("logonName"):
        return row["logonName"] == user_info["logonName"]
    return True


async def _read_record(client, today, page, uuid):
    from . import main

    listed = await client.my_reservations(today, page)
    records = main._reservation_records(listed, client.user_info)
    raw = next((r for r in listed["data"] if isinstance(r, dict)
                and str(r.get("uuid") or "") == uuid), {})
    return (next((r for r in records if r["uuid"] == uuid), None),
            _owns_record(raw, client.user_info))


async def _operation_client(session_id, account):
    from . import main

    try:
        return await main.authenticated_api(session_id, account)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="登录会话不存在或不属于当前账号") from exc
        raise


@router.post("/api/reservations/temp-leave")
async def temp_leave(
    request: TempLeaveRequest,
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    from . import main

    if not request.confirm:
        raise HTTPException(status_code=400, detail="请明确确认暂离")
    runtime = await main.ensure_runtime(account)
    if runtime.operation_lock.locked():
        raise HTTPException(status_code=409, detail="上一个预约操作仍在进行，请稍候")
    async with runtime.operation_lock:
        client = await _operation_client(request.auth_session_id, account)
        resv_id = None
        outcome = "未提交"
        try:
            today = library.beijing_now().date()
            own = True

            async def read_record():
                nonlocal own
                record, own = await _read_record(client, today, request.page, request.uuid)
                return record

            try:
                record = await read_record()
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=502, detail="查询今日预约失败，请稍后重试") from exc
            if record is None:
                raise HTTPException(status_code=404, detail="当前账号的今日预约中未找到这条预约，请刷新列表")
            resv_id = record["resv_id"]
            # resvBeginTime may be "YYYY-MM-DD HH:MM:SS" or epoch milliseconds.
            begin = _parse_moment(record["begin_time"], today)
            if begin is None or begin.date() != today:
                raise HTTPException(status_code=409, detail="仅支持今天的预约暂离")
            if not record["temp_leave_allowed"]:
                if not own:
                    reason = "不是本人预约"
                elif record["finished"]:
                    reason = "已结束"
                elif record["on_temp_leave"]:
                    reason = "已在暂离中"
                else:
                    reason = "未签到"
                raise HTTPException(status_code=409, detail=f"这条预约不能暂离：{reason}")
            if resv_id is None:
                raise HTTPException(status_code=409, detail="这条预约缺少预约编号，无法暂离")
            try:
                result = await client.temp_leave(resv_id)
            except Exception:
                outcome = "结果未确认"
                try:
                    record = await read_record()
                except Exception:
                    record = None
                if not record or not own or not record["on_temp_leave"]:
                    raise HTTPException(status_code=502, detail="暂离结果未确认，请刷新我的预约查看")
                message = "已确认暂离成功"
            else:
                if result.get("code") != 0:
                    outcome = "图书馆拒绝"
                    raise HTTPException(status_code=409, detail=result.get("message") or "暂离失败")
                message = result.get("message") or "暂离成功"
                # A confirmed action stays successful even if the deadline query fails.
                try:
                    record = await read_record()
                except Exception:
                    record = None
            outcome = "成功"
            return {"success": True, "message": message,
                    "temp_leave_end_time": record.get("temp_leave_end_time") if record else None}
        finally:
            main.logger.info("reservation temp leave: account=%s resv_id=%s result=%s",
                             account.username, resv_id, outcome)
            await client.close()


class RebookRequest(TempLeaveRequest):
    delay_minutes: int = Field(default=120, ge=5, le=900, strict=True)
    dry_run: bool = False


def plan_rebook(record: dict, seat_view: dict, now: datetime, delay: int) -> dict | str:
    """Plan one replacement slot, or return a refusal reason."""
    day = now.date()
    begin = _parse_moment(record.get("begin_time"), day)
    end = _parse_moment(record.get("end_time"), day)
    if begin is None or end is None or begin.date() != day or end.date() != day:
        return "仅支持今天且时间完整的预约重约"
    if record.get("finished"):
        return "这条预约已结束，不能重约"
    if not record.get("rebook_action"):
        return "图书馆不允许取消或提前结束这条预约"
    if not 5 <= delay <= 900:
        return "延后分钟数须在 5 至 900 之间"
    # dev_status is not checked: while the user sits in their own seat it reads as occupied.
    if seat_view.get("open_state") != 1:
        return "座位当前未开放"
    rule = seat_view.get("reservation_rule") or {}
    step = max(1, int(rule.get("timeInterval") or 5))
    target = now + timedelta(minutes=delay)
    openings = seat_view.get("open_times") or []
    candidates = []
    for opening in openings:
        anchor = _parse_moment(opening.get("openStartTime"), day)
        bound = _parse_moment(opening.get("openEndTime"), day)
        if anchor is None or bound is None:
            continue
        ticks = math.ceil((target - anchor).total_seconds() / (step * 60))
        rounded = anchor + timedelta(minutes=ticks * step)
        if anchor <= rounded < bound:
            candidates.append(rounded)
    if not candidates:
        return "延后开始时间不在座位开放时间内"
    start = min(candidates)
    if start >= end:
        return "延后开始时间必须早于原预约结束时间"
    duration = (end - start).total_seconds() / 60
    if duration < int(rule.get("minResvTime") or 30):
        return "剩余预约时长不足最短预约时长"
    if duration > int(rule.get("maxResvTime") or 930):
        return "剩余预约时长超过最长预约时长"
    if end.second or end.microsecond:
        return "原预约结束时间不在分钟网格上"
    if not library.is_valid_time_range(seat_view, start.strftime("%H:%M"), end.strftime("%H:%M")):
        return "延后时段不在座位实际可选范围内"
    for other in seat_view.get("reservations") or []:
        if not isinstance(other, dict):
            return "无法确认该座位已有预约的时间"
        other_begin = _parse_moment(other.get("startTime", other.get("resvBeginTime")), day)
        other_end = _parse_moment(other.get("endTime", other.get("resvEndTime")), day)
        identifiers = [(other.get("uuid"), record.get("uuid")),
                       (other.get("resvId"), record.get("resv_id"))]
        exposed = [(a, b) for a, b in identifiers if a is not None and b is not None]
        if any(str(a) == str(b) for a, b in exposed):
            continue
        if not exposed and other_begin == begin and other_end == end:
            continue
        if other_begin is None or other_end is None:
            return "无法确认该座位已有预约的时间"
        if start < other_end and end > other_begin:
            return "延后时段与该座位其他预约冲突"
    seat = seat_view["dev_name"]

    def slot(first, last):
        return {"seat": seat, "begin": first.strftime("%Y-%m-%d %H:%M:%S"),
                "end": last.strftime("%Y-%m-%d %H:%M:%S")}
    return {"release_action": record["rebook_action"], "original": slot(begin, end),
            "new": slot(start, end)}


@router.post("/api/reservations/rebook")
async def rebook(request: RebookRequest, account: Account = Depends(current_account)) -> dict[str, Any]:
    from . import main

    runtime = await main.ensure_runtime(account)
    if not request.dry_run:
        if not runtime.settings.submit_enabled:
            raise HTTPException(status_code=403, detail="submit_enabled is false（预约提交未开启）")
        if not request.confirm:
            raise HTTPException(status_code=400, detail="请明确确认释放并延后重约")
    if runtime.operation_lock.locked():
        raise HTTPException(status_code=409, detail="上一个预约操作仍在进行，请稍候")
    async with runtime.operation_lock:
        client = await _operation_client(request.auth_session_id, account)
        seat_name, action, state = None, None, "refused"
        try:
            now = library.beijing_now()
            try:
                record, own = await _read_record(client, now.date(), request.page, request.uuid)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=502, detail="查询今日预约失败，请稍后重试") from exc
            if record is None or not own:
                raise HTTPException(status_code=404, detail="当前账号的今日预约中未找到这条预约，请刷新列表")
            seat_name = next((d["name"] for d in record["devices"] if d.get("name")), None)
            action = record["rebook_action"]
            if record["finished"] or action is None:
                raise HTTPException(status_code=409, detail="这条预约已结束或图书馆不允许取消、提前结束")
            room_id = record.get("room_id") or runtime.settings.room_id
            try:
                listed = await client.seats(now.date(), room_id=room_id)
                if listed.get("code") != 0:
                    raise ValueError("seat query failed")
                seats = library.build_room_seat_view(listed, day=now.date())
            except Exception as exc:
                raise HTTPException(status_code=502, detail="查询座位实时规则失败，请稍后重试") from exc
            seat = next((s for s in seats if s["dev_name"] == seat_name
                         and (record.get("dev_id") is None or str(s["dev_id"]) == str(record["dev_id"]))), None)
            if seat is None:
                raise HTTPException(status_code=409, detail="无法确定座位所在区域")
            plan = plan_rebook(record, seat, now, request.delay_minutes)
            if isinstance(plan, str):
                raise HTTPException(status_code=409, detail=plan)
            if request.dry_run:
                state = "dry_run"
                return {"dry_run": True, **plan}
            try:
                released = await (client.cancel_reservation(request.uuid) if action == "cancel"
                                  else client.end_ahead(request.uuid))
            except Exception as exc:
                raise HTTPException(status_code=409, detail="原预约未变更：释放请求异常，请刷新我的预约确认") from exc
            release_message = released.get("message") or "释放成功"
            if released.get("code") != 0:
                raise HTTPException(status_code=409, detail=f"原预约未变更：{released.get('message') or '图书馆拒绝释放'}")
            try:
                result = await client.reserve(seat["dev_id"], plan["new"]["begin"], plan["new"]["end"])
                success = result.get("code") == 0
                reserve_message = result.get("message") or ("预约成功" if success else "图书馆拒绝预约")
            except Exception:
                success = False
                reserve_message = "预约请求异常或超时"
            state = "rebooked" if success else "released_not_rebooked"
            if not success:
                try:
                    # New reservations can move to page 1; inspect all current pages.
                    page = 1
                    while True:
                        listed = await client.my_reservations(now.date(), page)
                        records = main._reservation_records(listed, client.user_info)
                        own_uuids = {str(r.get("uuid") or "") for r in listed["data"]
                                     if isinstance(r, dict) and _owns_record(r, client.user_info)}
                        if any(r["uuid"] in own_uuids and not r["finished"] and str(r["dev_id"]) == str(seat["dev_id"])
                               and _parse_moment(r["begin_time"], now.date()) == _parse_moment(plan["new"]["begin"], now.date())
                               and _parse_moment(r["end_time"], now.date()) == _parse_moment(plan["new"]["end"], now.date())
                               for r in records):
                            success, state = True, "rebooked"
                            break
                        if page * 20 >= listed.get("count", len(records)):
                            break
                        page += 1
                except Exception:
                    state = "unknown"
            message = ("延后重约成功" if success else
                       "原预约已释放，重约结果未确认，请查看我的预约" if state == "unknown" else
                       f"原预约已释放，重约失败：{reserve_message}")
            return {**plan, "state": state, "success": success, "message": message,
                    "release_message": release_message, "reserve_message": reserve_message}
        finally:
            main.logger.info("reservation rebook: account=%s seat=%s action=%s state=%s",
                             account.username, seat_name, action, state)
            await client.close()
