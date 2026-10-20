"""資料層與獎金規則：夥伴、店家名單、派工、拜訪回報、核實。

規則（金額、每日上限…）放在 rules.json，改那個檔就好，不用改程式。
"""
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

STATUSES = {
    "pending": "尚未拜訪",
    "visited": "已拜訪·沒興趣",
    "interested": "有興趣·待跟進",
    "sampled": "已留樣品／資料",
    "closed": "已成交 🎉",
    "invalid": "店家不存在／已歇業",
}


@dataclass
class Rules:
    visit_fee: int = 20            # 有效拜訪回報費（要有聯絡人＋電話）
    daily_visit_cap: int = 15      # 每人每天最多幾筆算錢，防灌水
    close_bonus: int = 400         # 成交獎金：核實時填實收金額，達門檻才發
    min_close_amount: int = 1000   # 成交獎金門檻（實收金額）
    per_worker: int = 10           # 每人同時最多幾家待拜訪
    notes: list = field(default_factory=list)  # 顯示給夥伴看的其他說明

    @classmethod
    def load(cls, path: Path):
        if not Path(path).exists():
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def commission_for(self, result: str, has_contact: bool, over_cap: bool) -> int:
        """這筆回報值多少錢。成交獎金不在這裡發，要等老闆核實實收金額。"""
        if result == "invalid" or not has_contact or over_cap:
            return 0
        return self.visit_fee


SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    phone TEXT, area TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT, area TEXT,
    address TEXT, phone TEXT, note TEXT, priority INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL, UNIQUE(name, area));
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers(id),
    shop_id INTEGER NOT NULL REFERENCES shops(id), status TEXT NOT NULL DEFAULT 'pending',
    assigned_at TEXT NOT NULL, UNIQUE(shop_id));
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    worker_id INTEGER NOT NULL, shop_id INTEGER NOT NULL, result TEXT NOT NULL,
    contact_name TEXT, contact_phone TEXT, note TEXT,
    commission INTEGER NOT NULL DEFAULT 0, order_amount INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_visits_worker ON visits(worker_id, created_at);
"""


def _now(clock=None) -> str:
    return (clock() if clock else datetime.now()).isoformat(timespec="seconds")


class Dispatch:
    def __init__(self, db_path: Path, rules: Rules = None, clock=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules = rules or Rules()
        self.clock = clock
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ── 夥伴 ──────────────────────────────────────────────
    def add_worker(self, name, phone="", area="") -> str:
        code = secrets.token_hex(3).upper()  # 6 碼，例如 A3F9C1
        with self.conn() as c:
            c.execute("INSERT INTO workers (code, name, phone, area, created_at) VALUES (?,?,?,?,?)",
                      (code, name, phone, area, _now(self.clock)))
        return code

    def worker_by_code(self, code: str):
        with self.conn() as c:
            return c.execute("SELECT * FROM workers WHERE code=? AND active=1",
                             ((code or "").strip().upper(),)).fetchone()

    def list_workers(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM workers ORDER BY id").fetchall()

    def set_worker_active(self, worker_id, active: bool):
        with self.conn() as c:
            c.execute("UPDATE workers SET active=? WHERE id=?", (1 if active else 0, worker_id))

    # ── 店家 ──────────────────────────────────────────────
    def add_shop(self, name, category="", area="", address="", phone="", note="", priority=3):
        if not name:
            return None
        with self.conn() as c:
            try:
                return c.execute("INSERT INTO shops (name, category, area, address, phone, note, priority, created_at)"
                                 " VALUES (?,?,?,?,?,?,?,?)",
                                 (name, category, area, address, phone, note, int(priority or 3),
                                  _now(self.clock))).lastrowid
            except sqlite3.IntegrityError:
                return None  # 同區同名，略過

    def import_csv_rows(self, rows) -> tuple:
        """rows：csv.DictReader 的結果。中英文欄名都接受。回傳 (新增, 略過)。"""
        added = skipped = 0
        pick = lambda r, *ks: next(((r.get(k) or "").strip() for k in ks if (r.get(k) or "").strip()), "")
        for r in rows:
            pr = pick(r, "優先", "priority") or "3"
            ok = self.add_shop(pick(r, "店名", "name"), pick(r, "類型", "category"), pick(r, "區域", "area"),
                               pick(r, "地址", "address"), pick(r, "電話", "phone"), pick(r, "備註", "note"),
                               int(pr) if pr.isdigit() else 3)
            added += bool(ok)
            skipped += not ok
        return added, skipped

    def unassigned_count(self):
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM shops s LEFT JOIN assignments a ON a.shop_id=s.id"
                             " WHERE a.id IS NULL").fetchone()["n"]

    # ── 派工 ──────────────────────────────────────────────
    def auto_dispatch(self, per_worker: int = None) -> dict:
        """還沒派出去的店，依區域分給還有空位的夥伴；夥伴沒填區域＝哪裡都可以派。
        先派有指定區域的夥伴，免得「哪裡都去」的人把別人的區域搶光。"""
        per = per_worker or self.rules.per_worker
        out = {}
        with self.conn() as c:
            workers = c.execute("SELECT * FROM workers WHERE active=1"
                                " ORDER BY CASE WHEN COALESCE(area,'')='' THEN 1 ELSE 0 END, id").fetchall()
            for w in workers:
                cur = c.execute("SELECT COUNT(*) n FROM assignments WHERE worker_id=? AND status='pending'",
                                (w["id"],)).fetchone()["n"]
                quota = per - cur
                if quota <= 0:
                    continue
                area = w["area"] or ""
                shops = c.execute("SELECT s.id FROM shops s LEFT JOIN assignments a ON a.shop_id=s.id"
                                  " WHERE a.id IS NULL AND (?='' OR s.area=?) ORDER BY s.priority, s.id LIMIT ?",
                                  (area, area, quota)).fetchall()
                for s in shops:
                    c.execute("INSERT INTO assignments (worker_id, shop_id, assigned_at) VALUES (?,?,?)",
                              (w["id"], s["id"], _now(self.clock)))
                if shops:
                    out[w["name"]] = len(shops)
        return out

    def worker_tasks(self, worker_id):
        with self.conn() as c:
            return c.execute("SELECT a.id AS assignment_id, a.status, s.* FROM assignments a"
                             " JOIN shops s ON s.id=a.shop_id WHERE a.worker_id=?"
                             " ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, s.priority, s.name",
                             (worker_id,)).fetchall()

    def assignment(self, aid):
        with self.conn() as c:
            return c.execute("SELECT a.*, s.name AS shop_name, s.area, s.address FROM assignments a"
                             " JOIN shops s ON s.id=a.shop_id WHERE a.id=?", (aid,)).fetchone()

    # ── 回報與獎金 ────────────────────────────────────────
    def visits_today(self, worker_id) -> int:
        today = _now(self.clock)[:10]
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM visits WHERE worker_id=? AND commission>0"
                             " AND substr(created_at,1,10)=?", (worker_id, today)).fetchone()["n"]

    def record_visit(self, aid, worker_id, result, contact_name="", contact_phone="", note="") -> dict:
        if result not in STATUSES or result == "pending":
            raise ValueError("回報結果不正確")
        a = self.assignment(aid)
        if not a or a["worker_id"] != worker_id:
            raise PermissionError("這家店不是派給你的")
        if a["status"] != "pending":
            raise ValueError("這家店已經回報過了")
        has_contact = bool(contact_name.strip() and contact_phone.strip())
        over_cap = self.visits_today(worker_id) >= self.rules.daily_visit_cap
        amount = self.rules.commission_for(result, has_contact, over_cap)
        with self.conn() as c:
            vid = c.execute("INSERT INTO visits (assignment_id, worker_id, shop_id, result, contact_name,"
                            " contact_phone, note, commission, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (aid, worker_id, a["shop_id"], result, contact_name.strip(), contact_phone.strip(),
                             note.strip(), amount, _now(self.clock))).lastrowid
            c.execute("UPDATE assignments SET status=? WHERE id=?", (result, aid))
        return {"visit_id": vid, "commission": amount, "has_contact": has_contact, "over_cap": over_cap}

    def verify_visit(self, visit_id, order_amount: int = 0) -> int:
        """老闆核實才算入帳。成交且實收達門檻才加發成交獎金。回傳核實後金額。"""
        with self.conn() as c:
            v = c.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
            if not v or v["verified"]:
                return v["commission"] if v else 0
            amount = v["commission"]
            if v["result"] == "closed" and order_amount >= self.rules.min_close_amount:
                amount += self.rules.close_bonus
            c.execute("UPDATE visits SET verified=1, commission=?, order_amount=? WHERE id=?",
                      (amount, order_amount, visit_id))
            return amount

    def unverified(self):
        with self.conn() as c:
            return c.execute("SELECT v.*, w.name AS worker_name, s.name AS shop_name FROM visits v"
                             " JOIN workers w ON w.id=v.worker_id JOIN shops s ON s.id=v.shop_id"
                             " WHERE v.verified=0 AND (v.commission>0 OR v.result='closed')"
                             " ORDER BY v.id DESC").fetchall()

    def worker_summary(self, worker_id) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT COUNT(*) visits, COALESCE(SUM(result='interested'),0) interested,"
                          " COALESCE(SUM(result='closed'),0) closed,"
                          " COALESCE(SUM(CASE WHEN verified THEN commission END),0) paid,"
                          " COALESCE(SUM(CASE WHEN NOT verified THEN commission END),0) waiting"
                          " FROM visits WHERE worker_id=?", (worker_id,)).fetchone()
        return dict(r)

    def dashboard(self) -> dict:
        with self.conn() as c:
            by_status = {r["status"]: r["n"] for r in
                         c.execute("SELECT status, COUNT(*) n FROM assignments GROUP BY status")}
            return {
                "workers": c.execute("SELECT COUNT(*) n FROM workers WHERE active=1").fetchone()["n"],
                "shops": c.execute("SELECT COUNT(*) n FROM shops").fetchone()["n"],
                "assigned": sum(by_status.values()),
                "by_status": by_status,
                "unpaid": c.execute("SELECT COALESCE(SUM(commission),0) n FROM visits WHERE verified=0").fetchone()["n"],
                "leaders": c.execute(
                    "SELECT w.name, w.code, w.area, COUNT(v.id) visits, COALESCE(SUM(v.result='closed'),0) closed,"
                    " COALESCE(SUM(CASE WHEN v.verified THEN v.commission END),0) paid"
                    " FROM workers w LEFT JOIN visits v ON v.worker_id=w.id"
                    " GROUP BY w.id ORDER BY closed DESC, paid DESC LIMIT 20").fetchall(),
            }

    def payout(self, month: str):
        """某月（YYYY-MM）已核實的獎金，每位夥伴一列，給你匯款或簽收用。"""
        with self.conn() as c:
            return c.execute("SELECT w.code, w.name, w.phone, COUNT(v.id) items, SUM(v.commission) amount"
                             " FROM visits v JOIN workers w ON w.id=v.worker_id"
                             " WHERE v.verified=1 AND v.commission>0 AND substr(v.created_at,1,7)=?"
                             " GROUP BY w.id ORDER BY w.name", (month,)).fetchall()
