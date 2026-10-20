"""派工、獎金、核實與網頁權限測試。跑法：python -m unittest discover -s tests"""
import csv
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from models import Dispatch, Rules  # noqa: E402

TODAY = datetime(2026, 5, 1, 10, 0, 0)


def make(tmp, **rules):
    return Dispatch(Path(tmp) / "t.db", Rules(**rules), clock=lambda: TODAY)


def load_example(d):
    with open(ROOT / "shops.example.csv", encoding="utf-8") as f:
        return d.import_csv_rows(csv.DictReader(f))


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_example_and_skip_duplicates(self):
        self.assertEqual(load_example(self.d), (5, 0))
        self.assertEqual(load_example(self.d), (0, 5))

    def test_import_english_headers(self):
        rows = csv.DictReader(io.StringIO("name,area,priority\n示範店,西區,x\n"))
        self.assertEqual(self.d.import_csv_rows(rows), (1, 0))

    def test_dispatch_by_area_and_priority(self):
        load_example(self.d)
        north = self.d.add_worker("北區夥伴", area="北區")
        anywhere = self.d.add_worker("全區夥伴")
        res = self.d.auto_dispatch(per_worker=10)
        self.assertEqual(res, {"北區夥伴": 2, "全區夥伴": 3})
        names = [t["name"] for t in self.d.worker_tasks(self.d.worker_by_code(north)["id"])]
        self.assertEqual(names, ["示範咖啡一號店", "示範民宿"])
        # 已派出的不會再派一次
        self.assertEqual(self.d.auto_dispatch(), {})
        self.assertEqual(self.d.unassigned_count(), 0)
        self.assertTrue(self.d.worker_by_code(anywhere.lower()))  # 代碼不分大小寫

    def test_quota_per_worker(self):
        load_example(self.d)
        self.d.add_worker("甲")
        self.assertEqual(self.d.auto_dispatch(per_worker=2), {"甲": 2})
        self.assertEqual(self.d.auto_dispatch(per_worker=2), {})

    def _one_task(self):
        load_example(self.d)
        code = self.d.add_worker("甲")
        w = self.d.worker_by_code(code)
        self.d.auto_dispatch()
        return w, self.d.worker_tasks(w["id"])

    def test_commission_needs_contact(self):
        w, tasks = self._one_task()
        r1 = self.d.record_visit(tasks[0]["assignment_id"], w["id"], "interested", "陳老闆", "0900-000-009")
        r2 = self.d.record_visit(tasks[1]["assignment_id"], w["id"], "interested", "陳老闆", "")
        r3 = self.d.record_visit(tasks[2]["assignment_id"], w["id"], "invalid", "某人", "0900-000-008")
        self.assertEqual((r1["commission"], r2["commission"], r3["commission"]), (20, 0, 0))

    def test_cannot_report_twice_or_others_task(self):
        w, tasks = self._one_task()
        other = self.d.worker_by_code(self.d.add_worker("乙"))
        aid = tasks[0]["assignment_id"]
        with self.assertRaises(PermissionError):
            self.d.record_visit(aid, other["id"], "visited")
        self.d.record_visit(aid, w["id"], "visited")
        with self.assertRaises(ValueError):
            self.d.record_visit(aid, w["id"], "closed", "陳", "0900-000-001")
        with self.assertRaises(ValueError):
            self.d.record_visit(tasks[1]["assignment_id"], w["id"], "亂填")

    def test_daily_cap(self):
        d = make(self.tmp.name + "cap", daily_visit_cap=2)
        load_example(d)
        w = d.worker_by_code(d.add_worker("甲"))
        d.auto_dispatch()
        amounts = [d.record_visit(t["assignment_id"], w["id"], "visited", "某", "0900-000-001")["commission"]
                   for t in d.worker_tasks(w["id"])]
        self.assertEqual(amounts, [20, 20, 0, 0, 0])

    def test_close_bonus_only_when_paid_enough(self):
        w, tasks = self._one_task()
        v1 = self.d.record_visit(tasks[0]["assignment_id"], w["id"], "closed", "陳", "0900-000-001")["visit_id"]
        v2 = self.d.record_visit(tasks[1]["assignment_id"], w["id"], "closed", "林", "0900-000-002")["visit_id"]
        self.assertEqual(self.d.verify_visit(v1, 1500), 420)
        self.assertEqual(self.d.verify_visit(v2, 500), 20)
        self.assertEqual(self.d.verify_visit(v1, 99999), 420)  # 重複核實不會再加
        s = self.d.worker_summary(w["id"])
        self.assertEqual((s["paid"], s["waiting"], s["closed"]), (440, 0, 2))

    def test_payout_by_month(self):
        w, tasks = self._one_task()
        vid = self.d.record_visit(tasks[0]["assignment_id"], w["id"], "interested", "陳", "0900-000-001")["visit_id"]
        self.assertEqual(self.d.payout("2026-05"), [])
        self.d.verify_visit(vid)
        rows = self.d.payout("2026-05")
        self.assertEqual((rows[0]["name"], rows[0]["amount"]), ("甲", 20))
        self.assertEqual(self.d.payout("2026-06"), [])

    def test_rules_file_loads(self):
        r = Rules.load(ROOT / "rules.json")
        self.assertEqual((r.visit_fee, r.close_bonus), (20, 400))
        self.assertEqual(Rules.load(Path(self.tmp.name) / "none.json").visit_fee, 20)


class WebTest(unittest.TestCase):
    def setUp(self):
        try:
            import app as app_mod
        except ImportError:
            self.skipTest("沒裝 flask")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make(self.tmp.name)
        load_example(self.d)
        self.code = self.d.add_worker("<script>x</script>")
        self.d.auto_dispatch()
        self.app = app_mod.create_app(self.d, "testtoken123", secret_key="k")
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_admin_requires_login(self):
        self.assertEqual(self.c.get("/admin").status_code, 302)
        self.assertEqual(self.c.post("/admin/login", data={"token": "wrong"}).status_code, 200)
        self.assertEqual(self.c.get("/admin").status_code, 302)
        self.c.post("/admin/login", data={"token": "testtoken123"})
        self.assertEqual(self.c.get("/admin").status_code, 200)

    def test_csrf_required(self):
        self.c.post("/admin/login", data={"token": "testtoken123"})
        self.assertEqual(self.c.post("/admin/dispatch", data={"per_worker": "5"}).status_code, 400)

    def test_worker_page_escapes_html(self):
        html = self.c.get(f"/w/{self.code}").get_data(as_text=True)
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(self.c.get("/w/ZZZZZZ").status_code, 404)

    def test_report_flow(self):
        w = self.d.worker_by_code(self.code)
        aid = self.d.worker_tasks(w["id"])[0]["assignment_id"]
        r = self.c.post(f"/w/{self.code}/report/{aid}",
                        data={"result": "interested", "contact_name": "陳", "contact_phone": "0900-000-001"})
        self.assertIn("20 元", r.get_data(as_text=True))
        self.assertEqual(self.c.post(f"/w/{self.code}/report/{aid}", data={"result": "visited"}).status_code, 400)

    def test_admin_pages_render_and_actions(self):
        self.c.post("/admin/login", data={"token": "testtoken123"})
        with self.c.session_transaction() as s:
            csrf = s["csrf"]
        for path in ("/admin", "/admin/workers", "/admin/shops", "/admin/payout.csv?month=2026-05"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        r = self.c.post("/admin/workers", data={"csrf": csrf, "name": "新夥伴", "area": "東區"})
        self.assertIn("已建立", r.get_data(as_text=True))
        r = self.c.post("/admin/shops", data={"csrf": csrf, "csv": "店名,區域\n新店,東區"})
        self.assertIn("新增 1 家", r.get_data(as_text=True))
        r = self.c.post("/admin/dispatch", data={"csrf": csrf, "per_worker": "5"})
        self.assertIn("新夥伴：1 家", r.get_data(as_text=True))

    def test_login_throttle(self):
        for _ in range(8):
            self.assertIn("密碼不對", self.c.post("/admin/login", data={"token": "wrong"}).get_data(as_text=True))
        r = self.c.post("/admin/login", data={"token": "testtoken123"})  # 就算密碼對也先鎖住
        self.assertIn("嘗試次數太多", r.get_data(as_text=True))
        self.assertEqual(self.c.get("/admin").status_code, 302)

    def test_reported_task_redirects_and_404_is_chinese(self):
        w = self.d.worker_by_code(self.code)
        aid = self.d.worker_tasks(w["id"])[0]["assignment_id"]
        self.assertEqual(self.c.get(f"/w/{self.code}/report/{aid}").status_code, 200)
        self.c.post(f"/w/{self.code}/report/{aid}", data={"result": "visited"})
        self.assertEqual(self.c.get(f"/w/{self.code}/report/{aid}").status_code, 302)
        self.assertIn("找不到", self.c.get("/nope").get_data(as_text=True))

    def test_payout_csv_neutralises_formulas(self):
        evil = self.d.add_worker("=HYPERLINK(\"http://x\")", "+1", "特區")
        self.d.add_shop("特店", area="特區")
        w = self.d.worker_by_code(evil)
        self.d.auto_dispatch()
        aid = self.d.worker_tasks(w["id"])[0]["assignment_id"]
        vid = self.d.record_visit(aid, w["id"], "interested", "陳", "0900-000-001")["visit_id"]
        self.d.verify_visit(vid)
        self.c.post("/admin/login", data={"token": "testtoken123"})
        text = self.c.get("/admin/payout.csv?month=2026-05").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(",=HYPERLINK", text)

    def test_weak_token_rejected(self):
        import app as app_mod
        with self.assertRaises(ValueError):
            app_mod.create_app(self.d, "short")


if __name__ == "__main__":
    unittest.main()
