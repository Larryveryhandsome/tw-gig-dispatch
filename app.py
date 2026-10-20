"""地推／外包派工網頁：夥伴手機看任務、回報結果；管理端派工、核實、結算。

啟動：python app.py（Windows 背景執行請雙擊「背景啟動.vbs」，不會跳黑色視窗）
"""
import csv
import hmac
import io
import os
import secrets
import sys
from datetime import date
from pathlib import Path

from flask import Flask, Response, abort, redirect, render_template_string, request, session, url_for

from models import BASE_DIR, STATUSES, Dispatch, Rules


def load_dotenv(path=BASE_DIR / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


CSS = """*{box-sizing:border-box}body{font-family:-apple-system,"Noto Sans TC","Microsoft JhengHei",sans-serif;
background:#F5F3EC;color:#222;line-height:1.6;padding:16px;max-width:680px;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 4px;color:#1F5F7A}h2{font-size:1.05rem;margin:20px 0 8px;color:#1F5F7A}
.sub{color:#666;font-size:.85rem;margin-bottom:14px}.card{background:#fff;border-radius:12px;padding:14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h3{font-size:1rem;margin:0 0 2px}.meta{font-size:.82rem;color:#666}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.74rem;font-weight:600;background:#eee}
.b-interested{background:#FFF3CD}.b-sampled{background:#D6E9FF}.b-closed{background:#D6F5E3}.b-visited,.b-invalid{background:#F0E0E0}
a.btn,button{display:block;width:100%;padding:12px;border:0;border-radius:10px;background:#1F5F7A;color:#fff;font-size:1rem;
font-weight:600;text-align:center;text-decoration:none;margin-top:10px;cursor:pointer;font-family:inherit}
.ghost{background:#fff!important;color:#1F5F7A!important;border:1.5px solid #1F5F7A!important}
input,select,textarea{width:100%;padding:10px;border:1.5px solid #ddd;border-radius:9px;font-size:1rem;margin-top:6px;font-family:inherit}
label{display:block;margin-top:12px;font-size:.9rem;font-weight:600}
.stat{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.stat div{flex:1;min-width:90px;background:#fff;border-radius:10px;padding:10px;text-align:center}
.stat b{display:block;font-size:1.35rem;color:#1F5F7A}.stat span{font-size:.75rem;color:#666}
.tbl{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:.85rem;background:#fff}
th,td{padding:8px;text-align:left;border-bottom:1px solid #eee}th{background:#1F5F7A;color:#fff;font-size:.78rem}
.money{color:#9A6B00;font-weight:700}.tip{background:#FFF8E1;border-left:3px solid #E0A800;padding:10px;border-radius:6px;font-size:.85rem;margin:12px 0}
.row{display:flex;gap:6px}.row>*{flex:1}.small{padding:6px!important;font-size:.8rem!important;margin:0!important}"""

BASE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}｜{{ brand }}</title>
<style>""" + CSS + """</style></head><body>{% block body %}{% endblock %}</body></html>"""


def page(body_tpl, **ctx):
    tpl = BASE.replace("{% block body %}{% endblock %}", body_tpl)
    return render_template_string(tpl, **ctx)


def create_app(dispatch: Dispatch, admin_token: str, brand: str = "示範派工", secret_key: str = None):
    if not admin_token or len(admin_token) < 8:
        raise ValueError("管理密碼 DISPATCH_ADMIN_TOKEN 至少 8 個字")
    app = Flask(__name__)
    app.secret_key = secret_key or secrets.token_hex(32)
    R = dispatch.rules

    def ctx(**kw):
        kw.setdefault("brand", brand)
        kw.setdefault("STATUSES", STATUSES)
        kw.setdefault("R", R)
        return kw

    def need_admin():
        if not session.get("admin"):
            abort(redirect(url_for("admin_login")))

    def check_csrf():
        if request.form.get("csrf") != session.get("csrf"):
            abort(400)

    # ── 夥伴端 ──────────────────────────────────────────────
    @app.get("/")
    def home():
        return page("""<h1>{{ brand }}</h1><div class="sub">夥伴任務系統</div>
<form method="get" action="/go"><label>輸入你的夥伴代碼<input name="code" placeholder="例：A3F9C1" required autocapitalize="characters"></label>
<button>看我的任務</button></form><div class="tip">代碼是管理員發給你的 6 碼英數字，請不要給別人。</div>""",
                    **ctx(title="夥伴入口"))

    @app.get("/go")
    def go():
        w = dispatch.worker_by_code(request.args.get("code", ""))
        if not w:
            return page("""<h1>找不到這個代碼</h1><div class="sub">確認一下有沒有打錯。</div><a class="btn" href="/">回上一頁</a>""",
                        **ctx(title="找不到代碼")), 404
        return redirect(url_for("tasks", code=w["code"]))

    @app.get("/w/<code>")
    def tasks(code):
        w = dispatch.worker_by_code(code) or abort(404)
        rows = dispatch.worker_tasks(w["id"])
        return page("""<h1>嗨，{{ w.name }}</h1><div class="sub">代碼 {{ w.code }}{% if w.area %}　·　{{ w.area }}{% endif %}</div>
<div class="stat"><div><b>{{ pending|length }}</b><span>待拜訪</span></div><div><b>{{ s.interested }}</b><span>有興趣</span></div>
<div><b>{{ s.closed }}</b><span>已成交</span></div><div><b class="money">{{ s.paid }}</b><span>已核實獎金</span></div>
<div><b class="money">{{ s.waiting }}</b><span>待核實</span></div></div>
<div class="tip"><b>獎金規則</b><br>・有效拜訪回報 <b>{{ R.visit_fee }} 元/家</b>（要有聯絡人姓名＋電話，每天最多算 {{ R.daily_visit_cap }} 家）<br>
・成交獎金 <b>{{ R.close_bonus }} 元</b>（該店實收滿 {{ R.min_close_amount }} 元、款項入帳並核實後發放）
{% for n in R.notes %}<br>・{{ n }}{% endfor %}<br>沒問到聯絡人的不計酬，請誠實回報。</div>
<h2>待拜訪（{{ pending|length }}）</h2>
{% for t in pending %}<div class="card"><h3>{{ t.name }}</h3><div class="meta">{{ t.category or "—" }}｜{{ t.area or "—" }}{% if t.address %}｜{{ t.address }}{% endif %}</div>
{% if t.phone %}<div class="meta">☎ {{ t.phone }}</div>{% endif %}<a class="btn" href="/w/{{ w.code }}/report/{{ t.assignment_id }}">回報結果</a></div>
{% else %}<div class="card">目前沒有待拜訪的店，等下一批派工。</div>{% endfor %}
<h2>已回報（{{ done|length }}）</h2>
{% for t in done %}<div class="card"><h3>{{ t.name }}</h3><span class="badge b-{{ t.status }}">{{ STATUSES[t.status] }}</span></div>
{% else %}<div class="card">還沒有回報紀錄。</div>{% endfor %}""",
                    **ctx(title="我的任務", w=w, s=dispatch.worker_summary(w["id"]),
                          pending=[t for t in rows if t["status"] == "pending"],
                          done=[t for t in rows if t["status"] != "pending"]))

    @app.route("/w/<code>/report/<int:aid>", methods=["GET", "POST"])
    def report(code, aid):
        w = dispatch.worker_by_code(code) or abort(404)
        a = dispatch.assignment(aid)
        if not a or a["worker_id"] != w["id"]:
            abort(404)
        if request.method == "POST":
            f = request.form
            try:
                r = dispatch.record_visit(aid, w["id"], f.get("result", ""), f.get("contact_name", ""),
                                          f.get("contact_phone", ""), f.get("note", ""))
            except (ValueError, PermissionError) as e:
                return page("""<h1>沒辦法回報</h1><div class="sub">{{ msg }}</div><a class="btn" href="/w/{{ code }}">回任務清單</a>""",
                            **ctx(title="無法回報", msg=str(e), code=w["code"])), 400
            return page("""<h1>回報完成 ✅</h1><div class="sub">{{ a.shop_name }}</div>
<div class="card"><h3>這次回報費：<span class="money">{{ r.commission }} 元</span></h3><div class="meta">
{% if r.commission %}待管理員核實後入帳。{% elif r.over_cap %}今天已達 {{ R.daily_visit_cap }} 家上限，這筆不計酬，但資料一樣有用，謝謝你。
{% elif not r.has_contact %}這筆沒有回報費，因為沒有留下聯絡人姓名與電話。{% else %}這筆沒有獎金，但資料很有用，謝謝你。{% endif %}</div></div>
{% if result == "closed" %}<div class="tip">你回報了成交 🎉 款項入帳、管理員核實後，會再加發 <b>{{ R.close_bonus }} 元</b>。</div>{% endif %}
<a class="btn" href="/w/{{ code }}">回任務清單</a>""", **ctx(title="回報完成", a=a, r=r, result=f.get("result"), code=w["code"]))
        return page("""<h1>{{ a.shop_name }}</h1><div class="sub">{{ a.area or "" }}　{{ a.address or "" }}</div>
<form method="post"><label>拜訪結果<select name="result" required>{% for k, v in STATUSES.items() if k != "pending" %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select></label>
<label>聯絡人姓名／稱謂<input name="contact_name" placeholder="例：陳老闆、王店長"></label>
<label>聯絡電話或 LINE<input name="contact_phone" placeholder="沒問到就留空"></label>
<label>備註（對方在意什麼？什麼時候回訪？）<textarea name="note" rows="3"></textarea></label>
<button>送出回報</button><a class="btn ghost" href="/w/{{ code }}">取消</a></form>
<div class="tip">聯絡人和電話是核實獎金的依據。問不到也請誠實回報。</div>""", **ctx(title="回報結果", a=a, code=w["code"]))

    # ── 管理端 ──────────────────────────────────────────────
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        err = ""
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("token", ""), admin_token):
                session["admin"] = True
                session["csrf"] = secrets.token_hex(16)
                return redirect(url_for("admin"))
            err = "密碼不對"
        return page("""<h1>管理登入</h1><div class="sub">{{ err }}</div><form method="post">
<label>管理密碼<input name="token" type="password" required></label><button>登入</button></form>""", **ctx(title="管理登入", err=err))

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect(url_for("home"))

    @app.get("/admin")
    def admin():
        need_admin()
        return page("""<h1>派工儀表板</h1><div class="sub">{{ brand }}</div>
<div class="stat"><div><b>{{ d.workers }}</b><span>夥伴</span></div><div><b>{{ d.shops }}</b><span>店家名單</span></div>
<div><b>{{ d.assigned }}</b><span>已派工</span></div><div><b>{{ d.by_status.get("closed", 0) }}</b><span>已成交</span></div></div>
<div class="stat"><div><b>{{ d.by_status.get("pending", 0) }}</b><span>待拜訪</span></div><div><b>{{ d.by_status.get("interested", 0) }}</b><span>有興趣</span></div>
<div><b>{{ unassigned }}</b><span>還沒派</span></div><div><b class="money">{{ d.unpaid }}</b><span>待核實金額</span></div></div>
<form method="post" action="/admin/dispatch"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>每人同時最多幾家待拜訪<input name="per_worker" type="number" value="{{ R.per_worker }}" min="1" max="100"></label><button>自動派工</button></form>
<h2>待核實（{{ pend|length }}）</h2>
{% if pend %}<div class="tbl"><table><tr><th>夥伴</th><th>店家</th><th>結果</th><th>聯絡人</th><th>金額</th><th></th></tr>
{% for v in pend %}<tr><td>{{ v.worker_name }}</td><td>{{ v.shop_name }}</td><td>{{ STATUSES[v.result] }}</td><td>{{ v.contact_name }} {{ v.contact_phone }}</td>
<td class="money">{{ v.commission }}</td><td><form method="post" action="/admin/verify/{{ v.id }}"><input type="hidden" name="csrf" value="{{ csrf }}">
{% if v.result == "closed" %}<input name="order_amount" type="number" min="0" placeholder="實收金額" class="small">{% endif %}
<button class="small">核實</button></form></td></tr>{% endfor %}</table></div>{% else %}<div class="card">沒有待核實的回報。</div>{% endif %}
<h2>夥伴排行</h2><div class="tbl"><table><tr><th>姓名</th><th>代碼</th><th>區域</th><th>回報</th><th>成交</th><th>已核實</th></tr>
{% for r in d.leaders %}<tr><td>{{ r.name }}</td><td>{{ r.code }}</td><td>{{ r.area or "全區" }}</td><td>{{ r.visits }}</td><td>{{ r.closed }}</td><td class="money">{{ r.paid }}</td></tr>{% endfor %}</table></div>
<div class="row"><a class="btn ghost" href="/admin/workers">管理夥伴</a><a class="btn ghost" href="/admin/shops">匯入店家</a></div>
<a class="btn ghost" href="/admin/payout.csv?month={{ month }}">下載 {{ month }} 獎金結算 CSV</a>
<form method="post" action="/admin/logout"><button class="ghost">登出</button></form>""",
                    **ctx(title="派工儀表板", d=dispatch.dashboard(), pend=dispatch.unverified(),
                          unassigned=dispatch.unassigned_count(), csrf=session["csrf"], month=date.today().isoformat()[:7]))

    @app.post("/admin/dispatch")
    def do_dispatch():
        need_admin(); check_csrf()
        res = dispatch.auto_dispatch(int(request.form.get("per_worker") or R.per_worker))
        return page("""<h1>派工完成</h1><ul>{% for k, v in res.items() %}<li>{{ k }}：{{ v }} 家</li>{% else %}<li>沒有可派的店家</li>{% endfor %}</ul>
<a class="btn" href="/admin">回儀表板</a>""", **ctx(title="派工完成", res=res))

    @app.post("/admin/verify/<int:vid>")
    def do_verify(vid):
        need_admin(); check_csrf()
        raw = request.form.get("order_amount") or "0"
        dispatch.verify_visit(vid, int(raw) if raw.isdigit() else 0)
        return redirect(url_for("admin"))

    @app.route("/admin/workers", methods=["GET", "POST"])
    def admin_workers():
        need_admin()
        new_code = None
        if request.method == "POST":
            check_csrf()
            if request.form.get("toggle"):
                dispatch.set_worker_active(int(request.form["toggle"]), request.form.get("active") == "1")
            elif request.form.get("name", "").strip():
                new_code = dispatch.add_worker(request.form["name"].strip(), request.form.get("phone", "").strip(),
                                               request.form.get("area", "").strip())
        return page("""<h1>管理夥伴</h1>{% if new_code %}<div class="card"><h3>已建立</h3><div class="meta">代碼 <b>{{ new_code }}</b>，私下發給他。</div></div>{% endif %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><label>姓名<input name="name" required></label>
<label>負責區域（留空＝全區都可以派）<input name="area" placeholder="例如：北區、東區"></label><label>電話<input name="phone"></label><button>新增夥伴</button></form>
<h2>名單</h2><div class="tbl"><table><tr><th>姓名</th><th>代碼</th><th>區域</th><th>狀態</th></tr>
{% for w in ws %}<tr><td>{{ w.name }}</td><td><b>{{ w.code }}</b></td><td>{{ w.area or "全區" }}</td><td><form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="toggle" value="{{ w.id }}"><input type="hidden" name="active" value="{{ 0 if w.active else 1 }}">
<button class="small {{ '' if w.active else 'ghost' }}">{{ "啟用中（停用）" if w.active else "已停用（啟用）" }}</button></form></td></tr>{% endfor %}</table></div>
<a class="btn ghost" href="/admin">回儀表板</a>""", **ctx(title="管理夥伴", ws=dispatch.list_workers(), new_code=new_code, csrf=session["csrf"]))

    @app.route("/admin/shops", methods=["GET", "POST"])
    def admin_shops():
        need_admin()
        msg = None
        if request.method == "POST":
            check_csrf()
            raw = request.form.get("csv", "")
            up = request.files.get("file")
            if up and up.filename:
                raw = up.read().decode("utf-8-sig", "replace")
            msg = dispatch.import_csv_rows(csv.DictReader(io.StringIO(raw.strip())))
        return page("""<h1>匯入店家名單</h1><div class="sub">目前有 {{ un }} 家還沒派出去</div>
{% if msg %}<div class="card"><h3>匯入完成</h3><div class="meta">新增 {{ msg[0] }} 家，重複略過 {{ msg[1] }} 家。</div></div>{% endif %}
<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>上傳 CSV（UTF-8）<input type="file" name="file" accept=".csv"></label>
<label>或直接貼上（第一行是標題）<textarea name="csv" rows="8" placeholder="店名,類型,區域,地址,電話,備註,優先"></textarea></label><button>匯入</button></form>
<div class="tip">格式範例見專案裡的 <b>shops.example.csv</b>。欄位：店名、類型、區域、地址、電話、備註、優先（1 最優先）。</div>
<a class="btn ghost" href="/admin">回儀表板</a>""", **ctx(title="匯入店家", un=dispatch.unassigned_count(), msg=msg, csrf=session["csrf"]))

    @app.get("/admin/payout.csv")
    def payout_csv():
        need_admin()
        month = request.args.get("month", date.today().isoformat()[:7])[:7]
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["月份", "代碼", "姓名", "電話", "筆數", "金額"])
        for r in dispatch.payout(month):
            wr.writerow([month, r["code"], r["name"], r["phone"], r["items"], r["amount"]])
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=payout_{month}.csv"})

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main():
    load_dotenv()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    data_dir = Path(os.getenv("DISPATCH_DATA_DIR") or BASE_DIR / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:  # pythonw 背景執行：紀錄寫檔
        sys.stdout = sys.stderr = open(data_dir / "dispatch.log", "a", encoding="utf-8", buffering=1)
    key_file = data_dir / "secret.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    token = os.getenv("DISPATCH_ADMIN_TOKEN", "")
    if len(token) < 8:
        print("❌ 請在 .env 設定 DISPATCH_ADMIN_TOKEN（管理密碼，至少 8 個字）。見 README。")
        sys.exit(1)
    rules = Rules.load(BASE_DIR / "rules.json")
    app = create_app(Dispatch(data_dir / "dispatch.db", rules), token,
                     os.getenv("DISPATCH_BRAND", "示範派工"), key_file.read_text(encoding="utf-8").strip())
    host, port = os.getenv("DISPATCH_HOST", "127.0.0.1"), int(os.getenv("DISPATCH_PORT", "18801"))
    (data_dir / "dispatch.pid").write_text(str(os.getpid()), encoding="utf-8")
    print(f"派工系統啟動：夥伴入口 http://{host}:{port}/　管理 http://{host}:{port}/admin")
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
