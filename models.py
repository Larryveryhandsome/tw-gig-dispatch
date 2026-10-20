"""資料層與獎金規則：夥伴、店家名單、派工、拜訪回報、核實。

規則（金額、每日上限…）放在 rules.json，改那個檔就好，不用改程式。
"""
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TAIPEI = timezone(timedelta(hours=8))  # 台灣沒有夏令時間；伺服器放 UTC 的雲端主機也不會讓「今天」「本月」差 8 小時
MAX_AMOUNT = 100_000_000               # 實收金額上限（防手誤與整數溢位）

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
CREATE INDEX IF NOT EXISTS idx_visits_pending ON visits(verified, id);
CREATE INDEX IF NOT EXISTS idx_assign_worker ON assignments(worker_id, status);
CREATE INDEX IF NOT EXISTS idx_shops_area ON shops(area, priority, id);
"""
# 一張派工單只能有一筆回報：就算兩支手機同時送、或雙擊兩次，資料庫層也擋得住，不會重複計獎金
UNIQUE_VISIT = "CREATE UNIQUE INDEX IF NOT EXISTS uq_visits_assignment ON visits(assignment_id)"


def _now(clock=None) -> str:
    return (clock() if clock else datetime.now(TAIPEI).replace(tzinfo=None)).isoformat(timespec="seconds")


def _int(v, default=0, lo=None, hi=None):
    """只收 ASCII 數字（str.isdigit 會放過 '²' 之類，int() 反而丟例外變 500）。"""
    v = str(v if v is not None else "").strip()
    if not re.fullmatch(r"[0-9]{1,15}", v):
        return default
    n = int(v)
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _digits(v) -> str:
    return re.sub(r"\D", "", v or "")


class Dispatch:
    def __init__(self, db_path: Path, rules: Rules = None, clock=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules = rules or Rules()
        self.clock = clock
        with self.conn() as c:
            c.executescript(SCHEMA)
            try:
                c.execute(UNIQUE_VISIT)
            except sqlite3.IntegrityError:
                pass  # 舊資料庫裡已有重複回報：不阻擋啟動；record_visit 的交易仍會擋住新的重複

    def _connect(self):
        c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 30000")
        c.execute("PRAGMA journal_mode = WAL")  # 多支手機同時回報不互卡
        c.execute("PRAGMA foreign_keys = ON")
        return c

    @contextmanager
    def conn(self):
        """讀取用。"""
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self):
        """寫入用：BEGIN IMMEDIATE 先搶寫鎖，『檢查→寫入』整段不會被別人插隊（多人同時回報、同時派工）。"""
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    # ── 夥伴 ──────────────────────────────────────────────
    def add_worker(self, name, phone="", area="") -> str:
        code = secrets.token_hex(5).upper()  # 10 碼（約 1.1 兆種），例如 A3F9C1B27E；舊的 6 碼代碼照常可用
        with self.tx() as c:
            c.execute("INSERT INTO workers (code, name, phone, area, created_at) VALUES (?,?,?,?,?)",
                      (code, name[:60], phone[:30], area[:30], _now(self.clock)))
        return code

    def reset_worker_code(self, worker_id):
        """代碼外流或夥伴換手機：換一組新的，舊的立刻失效。回傳新代碼。"""
        code = secrets.token_hex(5).upper()
        with self.tx() as c:
            n = c.execute("UPDATE workers SET code=? WHERE id=?", (code, worker_id)).rowcount
        return code if n else None

    def set_worker_area(self, worker_id, area):
        with self.tx() as c:
            c.execute("UPDATE workers SET area=? WHERE id=?", ((area or "").strip()[:30], worker_id))

    def worker_by_code(self, code: str):
        with self.conn() as c:
            return c.execute("SELECT * FROM workers WHERE code=? AND active=1",
                             ((code or "").strip().upper(),)).fetchone()

    def list_workers(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM workers ORDER BY id").fetchall()

    def set_worker_active(self, worker_id, active: bool):
        with self.tx() as c:
            c.execute("UPDATE workers SET active=? WHERE id=?", (1 if active else 0, worker_id))
            if not active:
                # 停用（離職）：還沒拜訪的店退回待派，不會永遠卡在他名下
                c.execute("DELETE FROM assignments WHERE worker_id=? AND status='pending'", (worker_id,))

    # ── 店家 ──────────────────────────────────────────────
    def add_shop(self, name, category="", area="", address="", phone="", note="", priority=3, _c=None):
        if not name:
            return None
        row = (name[:100], category[:40], area[:30], address[:200], phone[:40], note[:500],
               _int(priority, 3, 1, 9) or 3, _now(self.clock))
        sql = ("INSERT OR IGNORE INTO shops (name, category, area, address, phone, note, priority, created_at)"
               " VALUES (?,?,?,?,?,?,?,?)")
        if _c is not None:
            cur = _c.execute(sql, row)
            return cur.lastrowid if cur.rowcount else None
        with self.tx() as c:
            cur = c.execute(sql, row)
            return cur.lastrowid if cur.rowcount else None  # 同區同名，略過

    def import_csv_rows(self, rows) -> tuple:
        """rows：csv.DictReader 的結果。中英文欄名都接受。回傳 (新增, 略過)。"""
        added = skipped = 0
        pick = lambda r, *ks: next(((r.get(k) or "").strip() for k in ks if (r.get(k) or "").strip()), "")
        with self.tx() as c:  # 整批一個交易：上千筆匯入不用每筆都等一次磁碟寫入
            for r in rows:
                ok = self.add_shop(pick(r, "店名", "name"), pick(r, "類型", "category"), pick(r, "區域", "area"),
                                   pick(r, "地址", "address"), pick(r, "電話", "phone"), pick(r, "備註", "note"),
                                   pick(r, "優先", "priority") or 3, _c=c)
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
        with self.tx() as c:  # 寫鎖：兩位管理員同時按「自動派工」也不會把同一家店派給兩個人
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
        contact_name, contact_phone, note = (contact_name or "").strip()[:60], (contact_phone or "").strip()[:40], (note or "").strip()[:500]
        # 聯絡人要像真的：姓名至少 1 個字；電話至少 7 碼數字（市話含區碼 9 碼、手機 10 碼），或 LINE ID 這類 3 字以上帶字母的
        phone_ok = len(_digits(contact_phone)) >= 7 or bool(re.search(r"[A-Za-z]", contact_phone) and len(contact_phone) >= 3)
        has_contact = bool(contact_name and phone_ok)
        with self.tx() as c:  # 檢查與寫入同一個交易：雙擊送出、兩支手機同時送都只會算一次
            a = c.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
            if not a or a["worker_id"] != worker_id:
                raise PermissionError("這家店不是派給你的")
            if a["status"] != "pending":
                raise ValueError("這家店已經回報過了")
            today = _now(self.clock)[:10]
            n_today = c.execute("SELECT COUNT(*) n FROM visits WHERE worker_id=? AND commission>0"
                                " AND substr(created_at,1,10)=?", (worker_id, today)).fetchone()["n"]
            over_cap = n_today >= self.rules.daily_visit_cap
            # 同一位夥伴反覆填同一支電話衝量：第二筆起不計回報費（成交獎金仍由老闆核實時決定）
            dup_phone = has_contact and bool(c.execute(
                "SELECT 1 FROM visits WHERE worker_id=? AND contact_phone=? LIMIT 1",
                (worker_id, contact_phone)).fetchone())
            amount = self.rules.commission_for(result, has_contact and not dup_phone, over_cap)
            vid = c.execute("INSERT INTO visits (assignment_id, worker_id, shop_id, result, contact_name,"
                            " contact_phone, note, commission, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (aid, worker_id, a["shop_id"], result, contact_name, contact_phone,
                             note, amount, _now(self.clock))).lastrowid
            c.execute("UPDATE assignments SET status=? WHERE id=?", (result, aid))
        return {"visit_id": vid, "commission": amount, "has_contact": has_contact, "over_cap": over_cap,
                "dup_phone": dup_phone}

    def verify_visit(self, visit_id, order_amount: int = 0) -> int:
        """老闆核實才算入帳。成交且實收達門檻才加發成交獎金。回傳核實後金額。"""
        order_amount = max(0, min(int(order_amount or 0), MAX_AMOUNT))
        with self.tx() as c:
            v = c.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
            if not v or v["verified"]:
                return v["commission"] if v else 0
            amount = v["commission"]
            if v["result"] == "closed" and order_amount >= self.rules.min_close_amount:
                amount += self.rules.close_bonus
            # 有條件更新：兩位管理員同時按核實，成交獎金只會加一次
            c.execute("UPDATE visits SET verified=1, commission=?, order_amount=? WHERE id=? AND verified=0",
                      (amount, order_amount, visit_id))
            return amount

    def unverified(self, limit=200):
        with self.conn() as c:
            return c.execute("SELECT v.*, w.name AS worker_name, s.name AS shop_name FROM visits v"
                             " JOIN workers w ON w.id=v.worker_id JOIN shops s ON s.id=v.shop_id"
                             " WHERE v.verified=0 AND (v.commission>0 OR v.result='closed')"
                             " ORDER BY v.id DESC LIMIT ?", (limit,)).fetchall()

    def unverified_count(self):
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM visits WHERE verified=0 AND (commission>0 OR result='closed')"
                             ).fetchone()["n"]

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
