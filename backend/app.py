
import hashlib
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 切窗值班写入门闩：同一时刻只允许一个值班落写，后手显式失败、排队重试，
# 绝不静默把后手写成成功。
_write_lock = threading.Lock()


def _now() -> datetime:
    """服务端权威时钟；测试可 monkeypatch app._now 演练到点前后。"""
    return datetime.now()


def now_str() -> str:
    return _now().isoformat(timespec="seconds")


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


@contextmanager
def write_gate():
    """切窗写入排队门闩：拿不到锁即 409，明示争用，禁止静默成功。"""
    if not _write_lock.acquire(blocking=False):
        raise HTTPException(409, "切窗争用：已有值班正在写入，后手请排队重试，禁止静默覆盖")
    try:
        yield
    finally:
        _write_lock.release()


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS window_bookings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT UNIQUE NOT NULL,
          close_at TEXT NOT NULL,
          policy TEXT NOT NULL CHECK(policy IN ('甲','乙')),
          note TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS late_frozen_enrollments(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT NOT NULL,
          slice_no TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy_deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT NOT NULL,
          policy_snapshot TEXT NOT NULL,
          deviation_type TEXT NOT NULL,
          detail TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")
        if conn.execute("SELECT COUNT(*) c FROM window_bookings").fetchone()["c"] == 0:
            # 示例预约：方案乙，关日时刻已过，页面开箱即可见“已关闭”门闩状态。
            conn.execute(
                "INSERT INTO window_bookings(biz_date,close_at,policy,note,created_by,created_at) VALUES (?,?,?,?,?,?)",
                ("2026-11-02", "2026-11-02T20:00:00", "乙", "示例：日切关窗预约，迟到数据只进冻结区", "admin", "2026-11-01T09:00:00"),
            )


@app.on_event("startup")
def _start():
    init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


# ---------------------------------------------------------------------------
# 切窗策略簿（预约）支撑函数
# ---------------------------------------------------------------------------

def get_booking(conn, biz_date: str):
    return conn.execute("SELECT * FROM window_bookings WHERE biz_date=?", (biz_date,)).fetchone()


def booking_closed(booking) -> bool:
    """到点判定一律以服务端时钟和策略簿 close_at 为准，前端说了不算。"""
    if booking is None:
        return False
    return _now() >= datetime.fromisoformat(booking["close_at"])


def booking_view(b) -> dict:
    d = dict(b)
    d["closed"] = booking_closed(b)
    return d


def log_deviation(conn, biz_date: str, policy: str, deviation_type: str, detail: str, operator: str):
    conn.execute(
        "INSERT INTO policy_deviations(biz_date,policy_snapshot,deviation_type,detail,operator,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (biz_date, policy or "", deviation_type, detail, operator, now_str()),
    )


def guard_window_closed(conn, biz_date: str, deviation_type: str, operator: str, action_desc: str):
    """普通写入门闩：该业务日切窗到点后，按策略簿口径一律拒绝并登记偏差。"""
    booking = get_booking(conn, biz_date)
    if booking_closed(booking):
        detail = (f"业务日 {biz_date} 切窗已于 {booking['close_at']} 到点关闭"
                  f"（方案{booking['policy']}），拒绝{action_desc}")
        log_deviation(conn, biz_date, booking["policy"], deviation_type, detail, operator)
        conn.commit()
        raise HTTPException(409, detail)
    return booking


def insert_reject_release(conn, sid: int, consumer: str, note: str):
    conn.execute(
        "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
        (sid, consumer, "拒绝", note, now_str()),
    )


# ---------------------------------------------------------------------------
# 切窗预约（策略簿）
# ---------------------------------------------------------------------------

class BookingIn(BaseModel):
    biz_date: str
    close_at: str
    policy: Literal["甲", "乙"]
    note: str = ""


@app.post("/api/window/bookings")
def create_booking(body: BookingIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    try:
        datetime.fromisoformat(body.close_at)
    except ValueError:
        raise HTTPException(400, "close_at 必须是 ISO 时间，例如 2026-11-16T18:00:00")
    with write_gate(), connect() as c:
        existing = get_booking(c, body.biz_date)
        if existing:
            if existing["policy"] != body.policy:
                # 同一业务日想换口径 = 混子窗，偏离已闩死的策略簿，拒绝并登记。
                detail = (f"业务日 {body.biz_date} 策略簿已闩死为方案{existing['policy']}，"
                          f"改口方案{body.policy} 属混子窗口径，当日禁止改口")
                log_deviation(c, body.biz_date, existing["policy"], "改口冲突", detail, operator)
                c.commit()
                raise HTTPException(409, detail)
            raise HTTPException(409, f"业务日 {body.biz_date} 切窗策略簿已存在（方案{existing['policy']}），当日禁止改口")
        cur = c.execute(
            "INSERT INTO window_bookings(biz_date,close_at,policy,note,created_by,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (body.biz_date, body.close_at, body.policy, body.note, operator, now_str()),
        )
        row = c.execute("SELECT * FROM window_bookings WHERE id=?", (cur.lastrowid,)).fetchone()
        return booking_view(row)


@app.get("/api/window/bookings")
def list_bookings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM window_bookings ORDER BY biz_date").fetchall()
    return {"items": [booking_view(r) for r in rows]}


@app.get("/api/window/bookings/{biz_date}")
def booking_detail(biz_date: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        b = get_booking(c, biz_date)
        if not b:
            raise HTTPException(404, "该业务日没有切窗预约")
        devs = c.execute(
            "SELECT * FROM policy_deviations WHERE biz_date=? ORDER BY id DESC", (biz_date,)
        ).fetchall()
        fzs = c.execute(
            "SELECT * FROM late_frozen_enrollments WHERE biz_date=? ORDER BY id DESC", (biz_date,)
        ).fetchall()
        rels = c.execute(
            """SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id
               WHERE s.biz_date=? ORDER BY r.id DESC""",
            (biz_date,),
        ).fetchall()
        sls = c.execute("SELECT * FROM slices WHERE biz_date=? ORDER BY id", (biz_date,)).fetchall()
    return {
        "booking": booking_view(b),
        "deviations": [dict(r) for r in devs],
        "frozen": [dict(r) for r in fzs],
        "releases": [dict(r) for r in rels],
        "slices": [dict(r) for r in sls],
    }


class FrozenIn(BaseModel):
    biz_date: str
    slice_no: str
    partition_key: str
    row_count: int
    expected_count: int
    note: str = ""


@app.post("/api/window/frozen-enrollments")
def enroll_frozen(body: FrozenIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    with write_gate(), connect() as c:
        booking = get_booking(c, body.biz_date)
        if not booking:
            raise HTTPException(409, f"业务日 {body.biz_date} 无切窗预约，迟到冻结区不予受理")
        if not booking_closed(booking):
            raise HTTPException(409, "切窗尚未到点关闭，迟到冻结区仅在到点后受理，请走正常切片登记")
        if booking["policy"] == "甲":
            # 方案甲到点后全只读，冻结报名也是写入，照样闩死。
            detail = f"业务日 {body.biz_date} 按方案甲到点后全量只读，迟到冻结报名一并拒绝"
            log_deviation(c, body.biz_date, "甲", "只读口径写入", detail, operator)
            c.commit()
            raise HTTPException(409, detail)
        # 方案乙唯一白名单写入：只落冻结区，永不触达 slices/releases/watermarks。
        cur = c.execute(
            "INSERT INTO late_frozen_enrollments(biz_date,slice_no,partition_key,row_count,expected_count,note,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (body.biz_date, body.slice_no, body.partition_key, body.row_count,
             body.expected_count, body.note, operator, now_str()),
        )
        return {"id": cur.lastrowid, "zone": "迟到冻结区", "released": False, "watermark_advanced": False}


@app.get("/api/window/frozen-enrollments")
def list_frozen(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM late_frozen_enrollments ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/window/deviations")
def list_deviations(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM policy_deviations ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 切片登记 / 裁决 / 放行（全部接入切窗门闩）
# ---------------------------------------------------------------------------

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else ("部分完整" if body.row_count > 0 else "待检")
    with write_gate(), connect() as c:
        guard_window_closed(c, body.biz_date, "关日后登记", operator, "切片登记写入")
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
            )
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")


@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    with write_gate(), connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        guard_window_closed(c, row["biz_date"], "关日后裁决", operator, "完整度裁决改写")
        comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else ("部分完整" if row["row_count"] > 0 else "待检")
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}


class ReleaseIn(BaseModel):
    consumer: str


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    with write_gate(), connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        booking = get_booking(c, row["biz_date"])
        if booking_closed(booking):
            # 到点后无论方案甲/乙：成功放行写入与水位推进一概拒绝，留拒绝流水并登记偏差。
            note = f"切窗到点关闭（方案{booking['policy']}）：禁止成功放行写入与水位推进"
            insert_reject_release(c, sid, body.consumer, note)
            log_deviation(
                c, row["biz_date"], booking["policy"], "关日后放行",
                f"对切片 {row['slice_no']} 的放行请求被切窗门闩拒绝，水位未推进", operator,
            )
            c.commit()
            raise HTTPException(409, note)
        if row["release_status"] == "已放行":
            # 已成功放行的记录禁止再写，防止任何形式的改写。
            insert_reject_release(c, sid, body.consumer, "切片已放行，禁止重复放行改写成功记录")
            c.commit()
            raise HTTPException(409, "切片已放行，禁止重复放行")
        if row["completeness"] != "完整":
            insert_reject_release(c, sid, body.consumer, "完整度未达标")
            c.commit()
            raise HTTPException(409, "未完整禁止放行")
        c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        c.execute(
            "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
            (sid, body.consumer, "成功", "完整切片放行", now_str()),
        )
        c.execute(
            "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
            (body.consumer, row["biz_date"], now_str(), row["slice_no"]),
        )
    return {"ok": True}


@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no, s.biz_date FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
