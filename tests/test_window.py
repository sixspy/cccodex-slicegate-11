
import threading
import time
from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import app, init_db, write_gate
import app as appmod


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def h(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def set_clock(monkeypatch, dt: datetime):
    monkeypatch.setattr(appmod, "_now", lambda: dt)


def make_booking(client, h, biz_date, policy, close_at):
    return client.post("/api/window/bookings", headers=h, json={
        "biz_date": biz_date, "close_at": close_at, "policy": policy, "note": "验收预约",
    })


# ---------------------------------------------------------------------------
# 策略簿：甲乙互斥、当日禁止改口、偏离子窗登记
# ---------------------------------------------------------------------------

def test_policy_book_locked_same_day_no_mix(client, h):
    r1 = make_booking(client, h, "2026-11-10", "乙", "2026-11-10T18:00:00")
    assert r1.status_code == 200
    # 同业务日改投方案甲 = 混子窗，拒绝并登记“改口冲突”
    r2 = make_booking(client, h, "2026-11-10", "甲", "2026-11-10T20:00:00")
    assert r2.status_code == 409
    # 同口径重复提交同样拒绝，策略簿只有一行
    r3 = make_booking(client, h, "2026-11-10", "乙", "2026-11-10T18:00:00")
    assert r3.status_code == 409
    items = client.get("/api/window/bookings", headers=h).json()["items"]
    assert len([b for b in items if b["biz_date"] == "2026-11-10"]) == 1
    devs = client.get("/api/window/deviations", headers=h).json()["items"]
    assert any(d["deviation_type"] == "改口冲突" and d["biz_date"] == "2026-11-10" for d in devs)


def test_booking_close_state_driven_by_server_clock(client, h, monkeypatch):
    make_booking(client, h, "2026-11-11", "甲", "2026-11-11T18:00:00")
    set_clock(monkeypatch, datetime(2026, 11, 11, 17, 0, 0))
    b = [x for x in client.get("/api/window/bookings", headers=h).json()["items"] if x["biz_date"] == "2026-11-11"][0]
    assert b["closed"] is False
    set_clock(monkeypatch, datetime(2026, 11, 11, 18, 0, 0))
    b = [x for x in client.get("/api/window/bookings", headers=h).json()["items"] if x["biz_date"] == "2026-11-11"][0]
    assert b["closed"] is True


# ---------------------------------------------------------------------------
# 方案甲：到点后该业务日全只读，一切写入闩死，页面查询照常
# ---------------------------------------------------------------------------

def test_plan_a_full_readonly_after_close(client, h, monkeypatch):
    date = "2026-11-12"
    make_booking(client, h, date, "甲", f"{date}T18:00:00")
    # 关日前先登记一个完整切片
    set_clock(monkeypatch, datetime(2026, 11, 12, 9, 0, 0))
    sid = client.post("/api/slices", headers=h, json={
        "slice_no": "SL-A-1", "biz_date": date, "partition_key": "east",
        "row_count": 10, "expected_count": 10,
    }).json()["id"]
    # 到点
    set_clock(monkeypatch, datetime(2026, 11, 12, 18, 30, 0))
    blocked = [
        client.post("/api/slices", headers=h, json={
            "slice_no": "SL-A-2", "biz_date": date, "partition_key": "west",
            "row_count": 1, "expected_count": 1}),
        client.post(f"/api/slices/{sid}/evaluate", headers=h),
        client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_a"}),
        client.post("/api/window/frozen-enrollments", headers=h, json={
            "biz_date": date, "slice_no": "SL-A-LATE", "partition_key": "east",
            "row_count": 3, "expected_count": 10}),
    ]
    assert all(r.status_code == 409 for r in blocked)
    # 只读查询不受影响
    assert client.get("/api/slices", headers=h).status_code == 200
    assert client.get(f"/api/window/bookings/{date}", headers=h).status_code == 200
    # 该业务日水位零推进（按测试业务日过滤，排除预置种子水位），切片没多出来，冻结区没写进去
    wm_rows = [w for w in client.get("/api/watermarks", headers=h).json()["items"] if w["biz_date"] == date]
    assert wm_rows == []
    assert all(s["slice_no"] != "SL-A-2" for s in client.get("/api/slices", headers=h).json()["items"])
    assert client.get("/api/window/frozen-enrollments", headers=h).json()["items"] == []
    # 偏差登记落账，且放行请求同时进了拒绝流水
    devs = client.get("/api/window/deviations", headers=h).json()["items"]
    types = {d["deviation_type"] for d in devs if d["biz_date"] == date}
    assert {"关日后登记", "关日后裁决", "关日后放行", "只读口径写入"} <= types
    rels = [r for r in client.get("/api/releases", headers=h).json()["items"] if r["biz_date"] == date]
    assert any(r["status"] == "拒绝" and "切窗到点关闭" in r["note"] for r in rels)


# ---------------------------------------------------------------------------
# 方案乙：到点后仅迟到冻结区可报名，成功放行与水位推进一概拒绝
# ---------------------------------------------------------------------------

def test_plan_b_only_frozen_zone_writable_after_close(client, h, monkeypatch):
    date = "2026-11-13"
    make_booking(client, h, date, "乙", f"{date}T18:00:00")
    set_clock(monkeypatch, datetime(2026, 11, 13, 9, 0, 0))
    sid = client.post("/api/slices", headers=h, json={
        "slice_no": "SL-B-1", "biz_date": date, "partition_key": "east",
        "row_count": 10, "expected_count": 10,
    }).json()["id"]
    wm_before = len(client.get("/api/watermarks", headers=h).json()["items"])
    # 到点
    set_clock(monkeypatch, datetime(2026, 11, 13, 18, 5, 0))
    # 普通登记与成功放行都被闩死
    assert client.post("/api/slices", headers=h, json={
        "slice_no": "SL-B-2", "biz_date": date, "partition_key": "west",
        "row_count": 1, "expected_count": 1}).status_code == 409
    rel = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_b"})
    assert rel.status_code == 409
    # 唯一白名单：迟到冻结报名成功，但永不触达放行/水位
    fz = client.post("/api/window/frozen-enrollments", headers=h, json={
        "biz_date": date, "slice_no": "SL-B-LATE", "partition_key": "east",
        "row_count": 7, "expected_count": 10})
    assert fz.status_code == 200
    body = fz.json()
    assert body["released"] is False and body["watermark_advanced"] is False
    wm_after = client.get("/api/watermarks", headers=h).json()["items"]
    assert len(wm_after) == wm_before
    assert all(w["source_slice"] != "SL-B-LATE" for w in wm_after)
    frozen = client.get("/api/window/frozen-enrollments", headers=h).json()["items"]
    assert any(f["slice_no"] == "SL-B-LATE" for f in frozen)
    # 冻结报名不会混进正式切片表
    assert all(s["slice_no"] != "SL-B-LATE" for s in client.get("/api/slices", headers=h).json()["items"])


def test_frozen_enrollment_requires_booking_and_closed_window(client, h, monkeypatch):
    # 无预约不受理
    r1 = client.post("/api/window/frozen-enrollments", headers=h, json={
        "biz_date": "2026-11-14", "slice_no": "X", "partition_key": "east",
        "row_count": 1, "expected_count": 1})
    assert r1.status_code == 409
    # 有预约但未到点：不受理，引导走正常登记
    make_booking(client, h, "2026-11-14", "乙", "2026-11-14T18:00:00")
    set_clock(monkeypatch, datetime(2026, 11, 14, 9, 0, 0))
    r2 = client.post("/api/window/frozen-enrollments", headers=h, json={
        "biz_date": "2026-11-14", "slice_no": "X", "partition_key": "east",
        "row_count": 1, "expected_count": 1})
    assert r2.status_code == 409
    assert "尚未到点" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# 两名值班并发：排队，后手显式 409 切窗争用，禁止静默写成成功
# ---------------------------------------------------------------------------

def test_write_gate_loser_gets_explicit_conflict():
    outcomes = []
    entered = threading.Event()

    def first():
        with write_gate():
            entered.set()
            time.sleep(0.2)
        outcomes.append("first-ok")

    def second():
        entered.wait(1)
        try:
            with write_gate():
                outcomes.append("second-ok")
        except HTTPException as e:
            outcomes.append((e.status_code, e.detail))

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start(); t2.start(); t1.join(); t2.join()
    # 线程 append 顺序不受调度保证，按内容断言：一个成功、一个显式争用失败
    assert "first-ok" in outcomes
    loser = [o for o in outcomes if isinstance(o, tuple)]
    assert len(loser) == 1 and loser[0][0] == 409 and "切窗争用" in loser[0][1]


def test_concurrent_booking_burst_exactly_one_winner(client, h):
    payload = {"biz_date": "2026-11-15", "close_at": "2026-11-15T18:00:00", "policy": "乙", "note": "并发"}
    codes = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        r = client.post("/api/window/bookings", headers=h, json=payload)
        codes.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(200) == 1
    assert all(code == 409 for code in codes if code != 200)
    rows = [b for b in client.get("/api/window/bookings", headers=h).json()["items"] if b["biz_date"] == "2026-11-15"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 到点后已成功放行的记录禁止改写，水位不回拨不推进
# ---------------------------------------------------------------------------

def test_released_slice_immutable_after_close(client, h, monkeypatch):
    date = "2026-11-16"
    make_booking(client, h, date, "乙", f"{date}T18:00:00")
    set_clock(monkeypatch, datetime(2026, 11, 16, 9, 0, 0))
    sid = client.post("/api/slices", headers=h, json={
        "slice_no": "SL-C-1", "biz_date": date, "partition_key": "east",
        "row_count": 5, "expected_count": 5,
    }).json()["id"]
    assert client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_c"}).status_code == 200
    wm_before = client.get("/api/watermarks", headers=h).json()["items"]
    set_clock(monkeypatch, datetime(2026, 11, 16, 19, 0, 0))
    # 到点后再放行：拒绝 + 偏差，已放行成功记录不动，水位不增
    r = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_c2"})
    assert r.status_code == 409
    wm_after = client.get("/api/watermarks", headers=h).json()["items"]
    assert wm_after == wm_before
    slice_row = [s for s in client.get("/api/slices", headers=h).json()["items"] if s["id"] == sid][0]
    assert slice_row["release_status"] == "已放行"
    devs = client.get("/api/window/deviations", headers=h).json()["items"]
    assert any(d["deviation_type"] == "关日后放行" and d["biz_date"] == date for d in devs)


# ---------------------------------------------------------------------------
# 预约列表 / 策略旁注 / 关日详情 / 放行流水 四视图对齐
# ---------------------------------------------------------------------------

def test_views_aligned_booking_detail_release_flow(client, h, monkeypatch):
    date = "2026-11-17"
    make_booking(client, h, date, "乙", f"{date}T18:00:00")
    set_clock(monkeypatch, datetime(2026, 11, 17, 9, 0, 0))
    sid = client.post("/api/slices", headers=h, json={
        "slice_no": "SL-D-1", "biz_date": date, "partition_key": "east",
        "row_count": 8, "expected_count": 8,
    }).json()["id"]
    client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_d"})
    set_clock(monkeypatch, datetime(2026, 11, 17, 18, 30, 0))
    client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_d"})  # 到点后拒绝
    client.post("/api/window/frozen-enrollments", headers=h, json={
        "biz_date": date, "slice_no": "SL-D-LATE", "partition_key": "west",
        "row_count": 2, "expected_count": 8})

    detail = client.get(f"/api/window/bookings/{date}", headers=h).json()
    # 关日详情里的流水与全局放行流水同源对齐
    global_rels = [r for r in client.get("/api/releases", headers=h).json()["items"] if r["biz_date"] == date]
    assert sorted(r["id"] for r in detail["releases"]) == sorted(r["id"] for r in global_rels)
    statuses = {r["status"] for r in detail["releases"]}
    assert statuses == {"成功", "拒绝"}
    # 预约列表与关日详情策略一致
    booking = [b for b in client.get("/api/window/bookings", headers=h).json()["items"] if b["biz_date"] == date][0]
    assert booking["policy"] == detail["booking"]["policy"] == "乙"
    assert booking["closed"] == detail["booking"]["closed"] is True
    # 冻结区：全局列表与关日详情对齐
    global_fz = [f for f in client.get("/api/window/frozen-enrollments", headers=h).json()["items"] if f["biz_date"] == date]
    assert [f["id"] for f in detail["frozen"]] == [f["id"] for f in global_fz]
    # 偏差：全局列表与关日详情对齐
    global_dev = [d for d in client.get("/api/window/deviations", headers=h).json()["items"] if d["biz_date"] == date]
    assert [d["id"] for d in detail["deviations"]] == [d["id"] for d in global_dev]
    # 切片视图对齐
    assert [s["id"] for s in detail["slices"]] == [
        s["id"] for s in client.get("/api/slices", headers=h).json()["items"] if s["biz_date"] == date]
