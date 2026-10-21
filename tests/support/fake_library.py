"""Stateful library double. Control routes are local QA helpers, never production APIs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException

from app.library import DEFAULT_TIME_RULE, beijing_now, is_valid_time_range


ROOM_ID = 102057443
ROOM_NAME = "图西楼四楼北区"
DEFAULT_USER = {"token": "qa-token", "accNo": "qa", "logonName": "qa", "userName": "测试用户"}


def ok(data: Any = None, message: str = "操作成功", **extra) -> dict:
    return {"code": 0, "message": message, "data": data, **extra}


def fail(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


class FakeLibrary:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.clock: datetime | None = None
        self.users = {DEFAULT_USER["token"]: dict(DEFAULT_USER)}
        self.rows: list[dict[str, Any]] = []
        self.failures: dict[str, dict] = {}
        # The live site may serialise resvBeginTime/resvEndTime as epoch milliseconds.
        self.epoch_times = False
        self.next_id = 1
        self.seats = [
            {"devId": 102057000 + number, "devName": f"F4B{number}",
             "roomId": ROOM_ID, "roomName": ROOM_NAME, "openState": 1, "devStatus": 0,
             "coordinate": f"{(number - 114) * 50},100", "devProp": 64,
             **deepcopy(DEFAULT_TIME_RULE)}
            for number in range(114, 121)
        ]

    def now(self) -> datetime:
        return self.clock if self.clock is not None else beijing_now()

    def user_for(self, account_id: str) -> dict:
        token = f"qa-{account_id}"
        return self.users.setdefault(token, {"token": token, "accNo": account_id,
                                           "logonName": account_id, "userName": "测试用户"})

    def seed(self, payload: dict) -> dict:
        token = payload.get("token") or next(reversed(self.users))
        user = self.users[token]
        seat = next(s for s in self.seats if s["devName"] == payload.get("seat", "F4B116"))
        day = payload.get("day", self.now().date().isoformat())
        if day == "today":
            day = self.now().date().isoformat()
        row = {
            "uuid": str(uuid4()), "resvId": self.next_id,
            "appAccNo": user["accNo"], "logonName": user["logonName"],
            "resvBeginTime": f"{day} {payload.get('begin', '08:00')}:00",
            "resvEndTime": f"{day} {payload.get('end', '22:00')}:00",
            "resvStatus": payload.get("status", 1 | 4 | 64),
            "endEarly": payload.get("endEarly", True), "tempLeaveEndTime": None,
            "resvDevInfoList": [{k: seat[k] for k in ("devId", "devName", "roomId", "roomName")}],
        }
        row.update(payload.get("row", {}))
        self.next_id += 1
        self.rows.append(row)
        return row

    async def failure(self, action: str, *, after: bool = False) -> dict | None:
        spec = self.failures.get(action)
        if not spec or bool(spec.get("after", False)) != after:
            return None
        self.failures.pop(action)
        if spec.get("mode") == "timeout":
            await asyncio.sleep(float(spec.get("seconds", 30)))
        return fail(spec.get("message", "模拟操作失败"))


def create_app(state: FakeLibrary | None = None) -> FastAPI:
    state = state or FakeLibrary()
    app = FastAPI(title="Fake USTB library")
    app.state.library = state

    async def user(token: str | None = Header(default=None)) -> dict:
        if token not in state.users:
            raise HTTPException(status_code=401, detail="测试 token 无效")
        return state.users[token]

    @app.post("/__fake/reset")
    async def reset():
        # Keep issued credentials usable by sessions already open in the QA UI.
        users = state.users.copy()
        state.reset()
        state.users.update(users)
        return ok()

    @app.get("/__fake/state")
    async def snapshot():
        return ok({"now": state.now().isoformat(), "users": list(state.users.values()),
                   "reservations": state.rows, "seats": state.seats})

    @app.post("/__fake/clock")
    async def clock(payload: dict):
        raw = payload.get("now")
        try:
            moment = datetime.fromisoformat(raw) if raw else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="测试时间格式无效") from exc
        if moment and moment.tzinfo is not None:
            raise HTTPException(status_code=422, detail="请使用无时区的北京时间")
        state.clock = moment
        return ok({"now": state.now().isoformat()})

    @app.post("/__fake/seed")
    async def seed(payload: dict):
        try:
            return ok(state.seed(payload))
        except (KeyError, StopIteration, ValueError) as exc:
            raise HTTPException(status_code=422, detail="测试用户或座位无效") from exc

    @app.post("/__fake/failures")
    async def failures(payload: dict):
        action = payload.get("action")
        if action not in {"reserve", "cancel", "endAhaed", "tempLeave"}:
            raise HTTPException(status_code=422, detail="测试操作无效")
        if payload.get("mode", "fail") not in {"fail", "timeout"}:
            raise HTTPException(status_code=422, detail="测试故障模式无效")
        state.failures[action] = dict(payload)
        return ok()

    @app.get("/ic-web/auth/userInfo")
    async def user_info(own: dict = Depends(user)):
        return ok(own)

    @app.get("/ic-web/device/tips")
    async def tips(devId: int, beginTime: str = "", own: dict = Depends(user)):
        return ok({"devId": devId, "beginTime": beginTime, "tips": "该时段可预约"})

    @app.get("/ic-web/seatMenu")
    async def menu(own: dict = Depends(user)):
        return ok([{"roomId": ROOM_ID, "roomName": ROOM_NAME}])

    @app.get("/ic-web/reserve/resvInfo")
    async def reservations(beginDate: str, endDate: str, page: int = 1, pageNum: int = 20,
                           own: dict = Depends(user)):
        rows = [r for r in state.rows if str(r["appAccNo"]) == str(own["accNo"])
                and beginDate <= r["resvBeginTime"][:10] <= endDate]
        rows = list(reversed(rows))
        page_rows = deepcopy(rows[(page - 1) * pageNum:page * pageNum])
        if state.epoch_times:
            for row in page_rows:
                for key in ("resvBeginTime", "resvEndTime"):
                    row[key] = int(datetime.fromisoformat(row[key]).timestamp() * 1000)
        return ok(page_rows, count=len(rows))

    @app.get("/ic-web/reserve")
    async def seats(roomIds: str, resvDates: str, own: dict = Depends(user)):
        day = datetime.strptime(resvDates, "%Y%m%d").date().isoformat()
        result = []
        for seat in state.seats:
            if str(seat["roomId"]) not in roomIds.split(","):
                continue
            rows = [r for r in state.rows if r["resvBeginTime"][:10] == day
                    and r["resvDevInfoList"][0]["devId"] == seat["devId"]
                    and not r["resvStatus"] & 128]
            result.append({**deepcopy(seat), "resvInfo": [
                {**deepcopy(r), "startTime": r["resvBeginTime"], "endTime": r["resvEndTime"]}
                for r in rows]})
        return ok(result)

    @app.post("/ic-web/reserve")
    async def reserve(payload: dict, own: dict = Depends(user)):
        injected = await state.failure("reserve")
        if injected is not None:
            return injected
        try:
            seat = next(s for s in state.seats if s["devId"] == payload["resvDev"][0])
            begin = datetime.fromisoformat(payload["resvBeginTime"])
            end = datetime.fromisoformat(payload["resvEndTime"])
        except (KeyError, ValueError, StopIteration, IndexError, TypeError):
            return fail("预约参数无效")
        if (str(payload.get("appAccNo")) != str(own["accNo"]) or begin.date() != end.date()
                or begin.second or begin.microsecond or end.second or end.microsecond
                or not is_valid_time_range(seat, begin.strftime("%H:%M"), end.strftime("%H:%M"))):
            return fail("预约时间不符合规则")
        for row in state.rows:
            if (not row["resvStatus"] & 128 and row["resvDevInfoList"][0]["devId"] == seat["devId"]
                    and begin < datetime.fromisoformat(row["resvEndTime"])
                    and end > datetime.fromisoformat(row["resvBeginTime"])):
                return fail("座位已被预约")
        row = state.seed({"token": own["token"], "seat": seat["devName"], "day": begin.date().isoformat(),
                          "begin": begin.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                          "status": 1 | 2, "endEarly": False})
        injected = await state.failure("reserve", after=True)
        return injected if injected is not None else ok(row, "预约成功")

    async def operate(action: str, payload: dict, own: dict):
        injected = await state.failure(action)
        if injected is not None:
            return injected
        key = "resvId" if action == "tempLeave" else "uuid"
        row = next((r for r in state.rows if str(r[key]) == str(payload.get(key))
                    and str(r["appAccNo"]) == str(own["accNo"])), None)
        if row is None:
            return fail("未找到本人的预约")
        status = row["resvStatus"]
        if action == "cancel":
            if status & (4 | 128):
                return fail("当前预约不能取消")
            state.rows.remove(row)
        elif action == "endAhaed":
            if not row["endEarly"] or status & 128:
                return fail("当前预约不能提前结束")
            row.update(resvStatus=status | 128, resvEndTime=state.now().strftime("%Y-%m-%d %H:%M:%S"),
                       endEarly=False)
        else:
            if not status & 64 or status & (128 | 2048):
                return fail("当前预约不能暂离")
            row.update(resvStatus=status | 2048,
                       tempLeaveEndTime=(state.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
        injected = await state.failure(action, after=True)
        return injected if injected is not None else ok(deepcopy(row))

    @app.post("/ic-web/reserve/delete")
    async def cancel(payload: dict, own: dict = Depends(user)):
        return await operate("cancel", payload, own)

    @app.post("/ic-web/reserve/endAhaed")
    async def end_ahead(payload: dict, own: dict = Depends(user)):
        return await operate("endAhaed", payload, own)

    @app.post("/ic-web/seatOperation/tempLeave")
    async def temp_leave(payload: dict, own: dict = Depends(user)):
        return await operate("tempLeave", payload, own)

    return app


app = create_app()
