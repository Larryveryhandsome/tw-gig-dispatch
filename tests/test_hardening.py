"""第二輪檢視補的測試：併發、重複計獎金、越權、防灌水、時區、輸入驗證、效能。
跑法：python -m unittest discover -s tests
"""
import csv
import io
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from models import Dispatch, Rules, _int  # noqa: E402

DAY = datetime(2026, 5, 1, 10, 0, 0)


def make(tmp, name="t.db", clock=lambda: DAY, **rules):
    return Dispatch(Path(tmp) / name, Rules(**rules), clock=clock)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, n, fn):
        barrier, out, errs = threading.Barrier(n), [], []

        def go(i):
            try:
                barrier.wait()
                out.append(fn(i))
            except Exception as e:  # noqa: BLE001
                out.append(e)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return out

    def test_double_submit_same_assignment_counts_once(self):
        """雙擊送出／兩支手機同時送同一家店：只能有一筆回報、只計一次回報費。"""
        self.d.add_shop("店A", area="北")
        w = self.d.worker_by_code(self.d.add_worker("甲"))
        self.d.auto_dispatch()
        aid = self.d.worker_tasks(w["id"])[0]["assignment_id"]
        out = self._run(12, lambda i: self.d.record_visit(aid, w["id"], "interested", "陳", "0900-123-456"))
        ok = [o for o in out if isinstance(o, dict)]
        self.assertEqual(len(ok), 1, out)
        self.assertTrue(all(isinstance(o, ValueError) for o in out if not isinstance(o, dict)), out)
        with self.d.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n, SUM(commission) s FROM visits").fetchone()["n"], 1)
        self.assertEqual(self.d.worker_summary(w["id"])["waiting"], 20)

    def test_db_level_unique_visit_per_assignment(self):
        self.d.add_shop("店A")
        w = self.d.worker_by_code(self.d.add_worker("甲"))
        self.d.auto_dispatch()
        aid = self.d.worker_tasks(w["id"])[0]["assignment_id"]
        self.d.record_visit(aid, w["id"], "visited", "陳", "0900-123-456")
        with self.d.conn() as c, self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO visits (assignment_id, worker_id, shop_id, result, created_at)"
                      " VALUES (?,?,?,?,?)", (aid, w["id"], 1, "visited", "x"))

    def test_many_workers_report_at_once_cap_respected(self):
        """同一位夥伴用多支手機同時回報：每日上限不能被併發繞過。"""
        d = make(self.tmp.name, "cap.db", daily_visit_cap=3)
        for i in range(10):
            d.add_shop(f"店{i}")
        w = d.worker_by_code(d.add_worker("甲"))
        d.auto_dispatch()
        tasks = d.worker_tasks(w["id"])
        out = self._run(10, lambda i: d.record_visit(tasks[i]["assignment_id"], w["id"], "visited", "某", f"0911-000-{i:03d}"))
        paid = sum(o["commission"] for o in out if isinstance(o, dict))
        self.assertEqual([o for o in out if not isinstance(o, dict)], [])
        self.assertEqual(paid, 3 * 20)

    def test_concurrent_dispatch_never_double_assigns(self):
        for i in range(40):
            self.d.add_shop(f"店{i}", area="北")
        for i in range(4):
            self.d.add_worker(f"夥伴{i}", area="北")
        out = self._run(6, lambda i: self.d.auto_dispatch(per_worker=5))
        self.assertEqual([o for o in out if isinstance(o, Exception)], [])
        with self.d.conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM assignments").fetchone()["n"]
            distinct = c.execute("SELECT COUNT(DISTINCT shop_id) n FROM assignments").fetchone()["n"]
            per = [r["n"] for r in c.execute("SELECT COUNT(*) n FROM assignments GROUP BY worker_id")]
        self.assertEqual(n, distinct)
        self.assertEqual(n, 20)
        self.assertTrue(all(p <= 5 for p in per), per)

    def test_double_verify_pays_bonus_once(self):
        d = make(self.tmp.name, "v.db")
        d.add_shop("店A")
        w = d.worker_by_code(d.add_worker("甲"))
        d.auto_dispatch()
        aid = d.worker_tasks(w["id"])[0]["assignment_id"]
        vid = d.record_visit(aid, w["id"], "closed", "陳", "0900-123-456")["visit_id"]
        out = self._run(8, lambda i: d.verify_visit(vid, 5000))
        self.assertEqual([o for o in out if isinstance(o, Exception)], [])
        self.assertEqual(d.worker_summary(w["id"])["paid"], 20 + 400)


class AntiGamingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make(self.tmp.name)
        for i in range(6):
            self.d.add_shop(f"店{i}")
        self.w = self.d.worker_by_code(self.d.add_worker("甲"))
        self.d.auto_dispatch()
        self.tasks = self.d.worker_tasks(self.w["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, i, name, phone):
        return self.d.record_visit(self.tasks[i]["assignment_id"], self.w["id"], "interested", name, phone)

    def test_fake_phone_not_paid(self):
        self.assertEqual(self.rec(0, "陳", "123")["commission"], 0)
        self.assertEqual(self.rec(1, "陳", "沒有")["commission"], 0)
        self.assertEqual(self.rec(2, "   ", "0912345678")["commission"], 0)
        self.assertEqual(self.rec(3, "陳", "0912-345-678")["commission"], 20)

    def test_line_id_accepted(self):
        self.assertEqual(self.rec(0, "陳老闆", "line: chen88")["commission"], 20)

    def test_same_phone_reused_only_pays_once(self):
        self.assertEqual(self.rec(0, "陳", "0912-345-678")["commission"], 20)
        r = self.rec(1, "陳", "0912-345-678")
        self.assertEqual(r["commission"], 0)
        self.assertTrue(r["dup_phone"])

    def test_cap_uses_taipei_day_not_server_utc_day(self):
        """UTC 伺服器在台灣 5/2 凌晨 01:00（UTC 5/1 17:00）：獎金統計算 5/2，不算 5/1。"""
        from datetime import timedelta, timezone
        import models
        fake_utc = datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_utc.astimezone(tz) if tz else fake_utc.replace(tzinfo=None)

        old = models.datetime
        models.datetime = FakeDT
        try:
            self.assertEqual(models._now()[:16], "2026-05-02T01:00")
        finally:
            models.datetime = old
        self.assertEqual(models.TAIPEI.utcoffset(None), timedelta(hours=8))

    def test_payout_month_boundary(self):
        clocks = {"t": datetime(2026, 4, 30, 23, 59, 0)}
        d = make(self.tmp.name, "m.db", clock=lambda: clocks["t"])
        d.add_shop("A"); d.add_shop("B")
        w = d.worker_by_code(d.add_worker("乙"))
        d.auto_dispatch()
        t = d.worker_tasks(w["id"])
        v1 = d.record_visit(t[0]["assignment_id"], w["id"], "visited", "陳", "0912-345-678")["visit_id"]
        clocks["t"] = datetime(2026, 5, 1, 0, 1, 0)
        v2 = d.record_visit(t[1]["assignment_id"], w["id"], "visited", "王", "0922-345-678")["visit_id"]
        d.verify_visit(v1); d.verify_visit(v2)
        self.assertEqual([(r["items"], r["amount"]) for r in d.payout("2026-04")], [(1, 20)])
        self.assertEqual([(r["items"], r["amount"]) for r in d.payout("2026-05")], [(1, 20)])


class DupPhoneRuleTest(unittest.TestCase):
    def _run(self, rule, clock_days=(1, 1)):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cur = {"d": datetime(2026, 5, clock_days[0], 10)}
        d = make(tmp.name, f"{rule}.db", clock=lambda: cur["d"], dup_phone_rule=rule)
        d.add_shop("連鎖一號店"); d.add_shop("連鎖二號店")
        w = d.worker_by_code(d.add_worker("甲"))
        d.auto_dispatch()
        t = d.worker_tasks(w["id"])
        a = d.record_visit(t[0]["assignment_id"], w["id"], "interested", "總部", "02-2345-6789")["commission"]
        cur["d"] = datetime(2026, 5, clock_days[1], 10)
        b = d.record_visit(t[1]["assignment_id"], w["id"], "interested", "總部", "02-2345-6789")["commission"]
        return a, b

    def test_default_is_same_day(self):
        self.assertEqual(Rules().dup_phone_rule, "same_day")

    def test_same_day_rule(self):
        self.assertEqual(self._run("same_day", (1, 1)), (20, 0))   # 同一天重複：不計
        self.assertEqual(self._run("same_day", (1, 2)), (20, 20))  # 隔天：連鎖店總機正常計

    def test_off_rule(self):
        self.assertEqual(self._run("off", (1, 1)), (20, 20))

    def test_always_rule(self):
        self.assertEqual(self._run("always", (1, 1)), (20, 0))
        self.assertEqual(self._run("always", (1, 2)), (20, 0))

    def test_invalid_rule_warns_and_falls_back_to_default(self):
        import contextlib
        import json
        for bad in ("sameday", "", "OFF", None, 5):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                r = Rules(dup_phone_rule=bad)
            self.assertEqual(r.dup_phone_rule, "same_day", bad)
            self.assertIn("dup_phone_rule", out.getvalue())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "rules.json"
        f.write_text(json.dumps({"dup_phone_rule": "sameday"}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            r = Rules.load(f)
        self.assertEqual(r.dup_phone_rule, "same_day")
        # 退回預設後，同一天同電話仍然被擋（防灌水沒失效）
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._run("sameday", (1, 1)), (20, 0))

    def test_rules_json_loads_rule(self):
        self.assertEqual(Rules.load(ROOT / "rules.json").dup_phone_rule, "same_day")


class AccessTest(unittest.TestCase):
    def setUp(self):
        try:
            import app as app_mod
        except ImportError:
            self.skipTest("沒裝 flask")
        self.app_mod = app_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make(self.tmp.name)
        for i in range(4):
            self.d.add_shop(f"店{i}", area="北")
        self.a = self.d.add_worker("甲", area="北")
        self.b = self.d.add_worker("乙", area="北")
        self.d.auto_dispatch(per_worker=2)
        self.app = app_mod.create_app(self.d, "testtoken123", secret_key="k")
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def login(self):
        self.c.post("/admin/login", data={"token": "testtoken123"})
        with self.c.session_transaction() as s:
            return s["csrf"]

    def test_idor_other_workers_assignment(self):
        wa, wb = self.d.worker_by_code(self.a), self.d.worker_by_code(self.b)
        b_aid = self.d.worker_tasks(wb["id"])[0]["assignment_id"]
        # 甲用自己的代碼，改網址 id 想看／回報乙的店
        self.assertEqual(self.c.get(f"/w/{self.a}/report/{b_aid}").status_code, 404)
        r = self.c.post(f"/w/{self.a}/report/{b_aid}", data={"result": "closed", "contact_name": "x", "contact_phone": "0912345678"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.d.worker_summary(wb["id"])["visits"], 0)
        self.assertIn("嗨，甲", self.c.get(f"/w/{self.a}").get_data(as_text=True))

    def test_reset_code_revokes_old_link(self):
        csrf = self.login()
        wa = self.d.worker_by_code(self.a)
        r = self.c.post("/admin/workers", data={"csrf": csrf, "reset": str(wa["id"])})
        self.assertEqual(r.status_code, 200)
        new = self.d.list_workers()[0]["code"]
        self.assertNotEqual(new, self.a)
        self.assertEqual(len(new), 10)
        c2 = self.app.test_client()
        self.assertEqual(c2.get(f"/w/{self.a}").status_code, 404)
        self.assertEqual(c2.get(f"/w/{new}").status_code, 200)

    def test_deactivate_returns_pending_shops_to_pool(self):
        wa = self.d.worker_by_code(self.a)
        before = self.d.unassigned_count()
        self.d.set_worker_active(wa["id"], False)
        self.assertEqual(self.d.unassigned_count(), before + 2)
        self.assertIsNone(self.d.worker_by_code(self.a))
        self.d.auto_dispatch(per_worker=4)
        self.assertEqual(self.d.unassigned_count(), 0)  # 乙補位

    def test_worker_code_guessing_is_throttled(self):
        for _ in range(20):
            self.assertEqual(self.c.get("/w/AAAAAAAAAA").status_code, 404)
        self.assertEqual(self.c.get("/w/BBBBBBBBBB").status_code, 429)
        self.assertEqual(self.c.get(f"/w/{self.a}").status_code, 429)  # 同一來源連真的也先擋

    def test_spoofed_proxy_header_cannot_bypass_login_lock(self):
        for i in range(8):
            self.c.post("/admin/login", data={"token": "bad"}, headers={"CF-Connecting-IP": f"1.2.3.{i}"})
        r = self.c.post("/admin/login", data={"token": "testtoken123"}, headers={"CF-Connecting-IP": "9.9.9.9"})
        self.assertIn("嘗試次數太多", r.get_data(as_text=True))

    def test_trusted_proxy_header_used_when_enabled(self):
        app = self.app_mod.create_app(self.d, "testtoken123", secret_key="k", trust_proxy_ip=True)
        c = app.test_client()
        for _ in range(8):
            c.post("/admin/login", data={"token": "bad"}, headers={"CF-Connecting-IP": "1.1.1.1"})
        r = c.post("/admin/login", data={"token": "testtoken123"}, headers={"CF-Connecting-IP": "2.2.2.2"})
        self.assertNotIn("嘗試次數太多", r.get_data(as_text=True))

    def test_bad_numbers_do_not_500(self):
        csrf = self.login()
        for path, data in (("/admin/dispatch", {"per_worker": "²"}), ("/admin/dispatch", {"per_worker": "abc"}),
                           ("/admin/verify/1", {"order_amount": "²³"}),
                           ("/admin/verify/1", {"order_amount": "9" * 40}),
                           ("/admin/workers", {"toggle": "²", "active": "1"})):
            r = self.c.post(path, data={"csrf": csrf, **data})
            self.assertLess(r.status_code, 500, (path, data, r.status_code))
        rows = csv.DictReader(io.StringIO("店名,優先\n怪店,²\n"))
        self.assertEqual(self.d.import_csv_rows(rows), (1, 0))

    def test_payout_month_validated_and_header_safe(self):
        self.login()
        self.assertEqual(self.c.get("/admin/payout.csv?month=2026-13").status_code, 400)
        self.assertEqual(self.c.get('/admin/payout.csv?month=26"x%0d%0aA').status_code, 400)
        self.assertEqual(self.c.get("/admin/payout.csv?month=2026-05").status_code, 200)

    def test_security_headers_and_upload_limit(self):
        r = self.c.get("/")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        csrf = self.login()
        big = io.BytesIO(b"a,b\n" + b"x" * (6 * 1024 * 1024))
        r = self.c.post("/admin/shops", data={"csrf": csrf, "file": (big, "big.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)

    def test_admin_can_set_area_from_ui(self):
        csrf = self.login()
        wa = self.d.worker_by_code(self.a)
        self.c.post("/admin/workers", data={"csrf": csrf, "set_area": str(wa["id"]), "area": "東區"})
        self.assertEqual(self.d.worker_by_code(self.a)["area"], "東區")

    def test_xss_in_shop_and_note_escaped(self):
        self.d.add_shop("<img src=x onerror=alert(1)>", area="北", note="<b>x</b>")
        self.d.add_worker("丙", area="北")
        self.d.auto_dispatch()
        code = self.d.list_workers()[-1]["code"]
        html = self.c.get(f"/w/{code}").get_data(as_text=True)
        self.assertNotIn("<img src=x", html)


class ScaleTest(unittest.TestCase):
    def test_thousands_of_shops_and_a_year_of_visits(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            d = make(tmp.name, "big.db", daily_visit_cap=10_000)
            rows = [{"店名": f"店{i}", "區域": f"區{i % 20}", "優先": str(i % 5 + 1)} for i in range(5000)]
            t0 = time.time()
            self.assertEqual(d.import_csv_rows(rows), (5000, 0))
            t_import = time.time() - t0
            for i in range(30):
                d.add_worker(f"夥伴{i}", area=f"區{i % 20}")
            t0 = time.time()
            d.auto_dispatch(per_worker=200)
            t_dispatch = time.time() - t0
            # 一年回報量：直接灌 100k 筆
            raw = sqlite3.connect(d.db_path)  # 灌假資料：略過外鍵與「一單一回報」限制，只為了量查詢速度
            raw.execute("DROP INDEX uq_visits_assignment")
            raw.executemany("INSERT INTO visits (assignment_id, worker_id, shop_id, result, contact_name, contact_phone,"
                            " commission, verified, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            [(100000 + i, 1 + i % 30, 1, "visited", "x", "0912345678", 20, i % 2,
                              f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}T10:00:00") for i in range(100_000)])
            raw.commit()
            raw.close()
            t0 = time.time()
            d.dashboard(); d.unverified(); d.unverified_count(); d.payout("2026-05"); d.worker_summary(1); d.worker_tasks(1)
            t_query = time.time() - t0
            print(f"\n[效能] 匯入5000店 {t_import:.2f}s／派工 {t_dispatch:.2f}s／10萬筆回報下的儀表板+結算查詢 {t_query:.2f}s")
            self.assertLess(t_import, 10)
            self.assertLess(t_dispatch, 10)
            self.assertLess(t_query, 5)
        finally:
            tmp.cleanup()


class IntTest(unittest.TestCase):
    def test_int_helper(self):
        self.assertEqual(_int("²", 7), 7)
        self.assertEqual(_int("１２", 7), 7)
        self.assertEqual(_int("12"), 12)
        self.assertEqual(_int("999", 0, 0, 100), 100)
        self.assertEqual(_int(None, 3), 3)


if __name__ == "__main__":
    unittest.main()
