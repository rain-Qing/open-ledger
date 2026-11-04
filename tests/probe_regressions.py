#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归探针：在临时目录起一个隔离实例，逐条验证审查报告里的缺陷是否已被修掉。

为什么单独一个文件：`tests/ledger_suite.py` 验的是"功能对不对"，这个文件验的是
"**曾经错的地方现在还会不会错**"——每条探针对应审查报告里的一个具体缺陷编号（R1/R2…），
修复前全 FAIL（证明尺子有效），修复后全 PASS（证明洞堵上了）。

用法：
    python tests/probe_regressions.py                 # 全跑
    python tests/probe_regressions.py --only P1,P6    # 只跑指定几条
    python tests/probe_regressions.py --src /opt/ledger   # 对线上部署的那份代码跑

退出码：0 = 全部 PASS ／ 1 = 有 FAIL ／ 2 = 实例起不来。
安全：只用 LEDGER_HOME 指向的临时目录，不读写生产库；跑完删目录、杀进程。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

SRC_DEFAULT = Path(__file__).resolve().parent.parent / "ledger" / "server.py"

# 只打 127.0.0.1，必须绕开环境里的 http_proxy（否则偶发 502 / 十几秒超时，
# 表现为"探针随机失败"）。理由与 tests/ledger_suite.py 同。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- HTTP 客户端

class Client:
    """极简客户端：每次请求新建连接，方便观察"连接被重置"这种失败。"""

    def __init__(self, base, label=""):
        self.base = base
        self.label = label
        self.sid = None

    def req(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.sid:
            headers["Cookie"] = "sid=" + self.sid
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with _OPENER.open(r, timeout=10) as resp:
                raw = resp.read()
                sc = resp.headers.get("Set-Cookie") or ""
                if "sid=" in sc:
                    self.sid = sc.split("sid=")[1].split(";")[0]
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:200]}
        except Exception as e:
            return 0, {"exc": type(e).__name__}

    def get(self, p):
        return self.req("GET", p)

    def post(self, p, b=None):
        return self.req("POST", p, b if b is not None else {})

    def patch(self, p, b):
        return self.req("PATCH", p, b)


# ---------------------------------------------------------------- 探针上下文

class Ctx:
    """一条隔离实例 + 一个普通账号 + 一个管理员账号。"""

    def __init__(self, base, admin, user, uid):
        self.base = base
        self.admin = admin
        self.u = user
        self.uid = uid
        self.today = time.strftime("%Y-%m-%d")
        self.month = self.today[:7]
        self.order = 0

    def add(self, cents, direction="expense", cat=("餐饮", "早餐"), **kw):
        body = dict(tx_date=self.today, direction=direction, cat_parent=cat[0],
                    category=cat[1], amount_cents=cents, owner_id=self.uid)
        body.update(kw)
        self.order += 1
        st, r = self.u.post("/api/tx", body)
        if st != 200:
            raise AssertionError("建账失败（%s）：%s %s" % (st, body, r))
        return r["id"]

    def refund(self, parent_id, cents):
        st, r = self.u.post("/api/tx", {"tx_date": self.today, "direction": "refund",
                                        "parent_id": parent_id,
                                        "amount_cents": cents, "owner_id": self.uid})
        if st != 200:
            raise AssertionError("记退款失败（%s）：%s" % (st, r))
        return r["id"]

    def row(self, tx_id):
        items = self.u.get("/api/tx?month=" + self.month)[1].get("items") or []
        for x in items:
            if x["id"] == tx_id:
                return x
        return None


# ---------------------------------------------------------------- 探针注册

PROBES = []


def probe(pid, title):
    def deco(fn):
        PROBES.append((pid, title, fn))
        return fn
    return deco


@probe("P1", "编辑空/非法日期必须被拒，记录不能从月度视图消失（R1）")
def p1(ctx):
    tid = ctx.add(12345)
    bad = []
    for v in ("", "abc", "2026-13-40"):
        st, body = ctx.u.patch("/api/tx/%d" % tid, {"tx_date": v})
        if st != 400:
            bad.append("%r -> %s" % (v, st))
    still = ctx.row(tid) is not None
    return (not bad) and still, "非法值: %s | 记录仍在当月列表=%s" % (bad or "全部 400", still)


@probe("P2", "支出金额不得改到低于已冲抵额，统计不能出现负支出（R2）")
def p2(ctx):
    pid = ctx.add(20000)
    ctx.refund(pid, 15000)
    st, body = ctx.u.patch("/api/tx/%d" % pid, {"amount_cents": 10000})
    row = ctx.row(pid) or {}
    stats = ctx.u.get("/api/stats?month=" + ctx.month)[1]
    ok = (st == 400 and row.get("amount_cents", 0) >= row.get("refunded_cents", 0)
          and stats.get("expense", 0) >= 0)
    return ok, "PATCH 200→100 = %s | amount=%s refunded=%s | stats.expense=%s" % (
        st, row.get("amount_cents"), row.get("refunded_cents"), stats.get("expense"))


@probe("P3", "脏 parent_id 必须 400，不能把连接打死（R4）")
def p3(ctx):
    tid = ctx.add(100)
    st, body = ctx.u.patch("/api/tx/%d" % tid, {"parent_id": "abc"})
    return st == 400, "PATCH parent_id='abc' -> %s %s" % (st, body)


@probe("P4", "有子冲抵的支出禁止改收支方向，冲抵不能悬空（R5）")
def p4(ctx):
    pid = ctx.add(5000)
    ctx.refund(pid, 2000)
    st, body = ctx.u.patch("/api/tx/%d" % pid, {
        "direction": "income", "cat_parent": "工资", "category": "月度工资"})
    return st == 400, "PATCH 支出→收入 -> %s %s" % (st, body)


@probe("P5", "非支出记录不得带 reimbursed，mark=reimb 不能筛出收入（R6）")
def p5(ctx):
    tid = ctx.add(8000, direction="income", cat=("工资", "月度工资"), reimbursed=True)
    row = ctx.row(tid) or {}
    hits = (ctx.u.get("/api/tx?mark=reimb")[1].get("items") or [])
    hit = any(x["id"] == tid for x in hits)
    return row.get("reimbursed") == 0 and not hit, "入库 reimbursed=%r | mark=reimb 命中=%s" % (
        row.get("reimbursed"), hit)


@probe("P6", "删一个分类同时加一个，不能被当成改名而改写历史（R3）")
def p6(ctx):
    cats = ctx.admin.get("/api/categories/config")[1].get("config") or {}
    exp = cats.get("expense") or {}
    if "餐饮" not in exp:
        raise AssertionError("测试库里没有「餐饮」分类，探针需要它")
    ctx.add(3000, cat=("餐饮", "午餐"))
    before = len(ctx.u.get("/api/tx?cat_parent=" + quote("餐饮"))[1].get("items") or [])
    new_exp = {k: v for k, v in exp.items() if k != "餐饮"}
    new_exp["外卖"] = {"color_slot": None, "subs": ["外卖", "其他"]}
    cats["expense"] = new_exp
    st, body = ctx.admin.post("/api/categories/config", {"config": cats})
    after_old = len(ctx.u.get("/api/tx?cat_parent=" + quote("餐饮"))[1].get("items") or [])
    after_new = len(ctx.u.get("/api/tx?cat_parent=" + quote("外卖"))[1].get("items") or [])
    rewritten = (body or {}).get("renamed_tx") or 0
    ok = (st == 400 and after_old == before and after_new == 0 and not rewritten)
    return ok, ("保存 -> %s renamed_tx=%s | 「餐饮」%d→%d 笔 | 「外卖」%d 笔"
                % (st, rewritten, before, after_old, after_new))


@probe("P7", "任意接口传 JSON 数组要返回 4xx，不能重置连接（R4）")
def p7(ctx):
    st, body = ctx.u.req("POST", "/api/tx", [1, 2])
    return 400 <= st < 500, "POST /api/tx [1,2] -> %s %s" % (st, body)


@probe("P8", "PATCH 金额必须与 POST 同一上限（100 万元）（R9）")
def p8(ctx):
    tid = ctx.add(100)
    st, body = ctx.u.patch("/api/tx/%d" % tid, {"amount_cents": 200_000_000})
    row = ctx.row(tid) or {}
    ok = st == 400 and row.get("amount_cents") == 100
    return ok, "PATCH 2 亿元 -> %s | 入库 amount=%s" % (st, row.get("amount_cents"))


@probe("Q1", "登录接口传 JSON 数组要返回 4xx，未鉴权也不能打崩连接（R4）")
def q1(ctx):
    anon = Client(ctx.base, "anon")
    st, body = anon.req("POST", "/api/login", [1, 2])
    return 400 <= st < 500, "POST /api/login [1,2] -> %s %s" % (st, body)


@probe("P9", "冲抵记录的归属强制跟随原单，改不动（R8）")
def p9(ctx):
    admin_id = ctx.admin.get("/api/me")[1]["user"]["id"]
    pid = ctx.add(10000, owner_id=admin_id)
    st, r = ctx.u.post("/api/tx", {"tx_date": ctx.today, "direction": "refund",
                                  "parent_id": pid, "amount_cents": 3000,
                                  "owner_id": ctx.uid})
    rid = r["id"]
    before = (ctx.row(rid) or {}).get("owner_id")
    st, body = ctx.u.patch("/api/tx/%d" % rid, {"owner_id": ctx.uid})
    after = (ctx.row(rid) or {}).get("owner_id")
    return (before == admin_id and after == admin_id), \
        "改归属 -> %s | owner %s → %s（原单归属 %s）" % (st, before, after, admin_id)


# ---------------------------------------------------------------- 起停实例

class SetupError(RuntimeError):
    """起实例 / 建账号阶段的失败——和"探针断言失败"要分开报。"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_instance(src):
    tmp = Path(tempfile.mkdtemp(prefix="ledger_probe_"))
    port = free_port()
    env = dict(os.environ, LEDGER_HOME=str(tmp), LEDGER_PORT=str(port))
    # 日志写文件不用 PIPE：PIPE 没人读，缓冲区满了子进程会阻塞在 write 上，
    # 现象是「探针随机卡十几秒」。S-04 加访问日志后踩到过。
    logpath = tmp / "server.log"
    logf = open(logpath, "wb")
    proc = subprocess.Popen([sys.executable, str(src)], env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            _OPENER.open(base + "/healthz", timeout=1).read()
            return tmp, proc, base
        except Exception:
            if proc.poll() is not None:
                logf.flush()
                out = logpath.read_text(encoding="utf-8", errors="replace")
                raise SetupError("实例启动即退出：\n" + out[-1500:])
            time.sleep(0.1)
    raise SetupError("实例 10 秒内没起来（端口 %d）" % port)


def stop_instance(tmp, proc):
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


def build_ctx(base, tmp):
    # S-05 起初始密码只在启动日志里打印一次，不再写 data/initial_admin.txt
    # （那个文件会跟着备份包走，等于明文密码跟着到处跑）。
    logpath = tmp / "server.log"
    pwd = ""
    for _ in range(50):
        txt = logpath.read_text(encoding="utf-8", errors="replace") if logpath.exists() else ""
        if "管理员账号 admin / " in txt:
            pwd = txt.split("管理员账号 admin / ", 1)[1].split()[0]
            break
        time.sleep(0.05)
    if not pwd:
        raise SetupError("启动日志里没找到初始密码")
    admin = Client(base, "admin")
    st, body = admin.post("/api/login", {"username": "admin", "password": pwd})
    if st != 200:
        raise SetupError("管理员登录失败：%s %s" % (st, body))
    st, body = admin.post("/api/users", {"username": "probe", "display_name": "探针",
                                         "password": "probe12345"})
    if st not in (200, 400):     # 400 = 已存在（重跑同目录时）
        raise SetupError("建普通账号失败：%s %s" % (st, body))
    user = Client(base, "probe")
    st, body = user.post("/api/login", {"username": "probe", "password": "probe12345"})
    if st != 200:
        raise SetupError("普通账号登录失败：%s %s" % (st, body))
    uid = user.get("/api/me")[1]["user"]["id"]
    return Ctx(base, admin, user, uid)


def run_one(fn, src):
    """每条探针一个全新实例：探针之间互不影响，单跑和全跑结果一致。

    P6 会改分类树、P3/P7 会把连接打崩——共用一个实例的话，后面的探针会被前面的
    副作用连累，排错时看不到真正原因。多花几秒起实例值得。
    """
    tmp, proc, base = start_instance(src)
    try:
        ctx = build_ctx(base, tmp)
        return fn(ctx)
    finally:
        stop_instance(tmp, proc)


def main():
    ap = argparse.ArgumentParser(description="账本回归探针")
    ap.add_argument("--only", default="", help="只跑指定探针，逗号分隔，如 P1,P6")
    ap.add_argument("--src", default=str(SRC_DEFAULT), help="server.py 路径")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print("找不到 server.py：%s" % src)
        return 2

    wanted = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    selected = [p for p in PROBES if not wanted or p[0] in wanted]
    if not selected:
        print("没有匹配的探针：%s" % args.only)
        return 2

    print("源文件: %s" % src)
    results = []
    for pid, title, fn in selected:
        try:
            ok, detail = run_one(fn, src)
        except SetupError as e:
            print("起实例失败，后面的探针不再跑：%s" % e)
            return 2
        except Exception:
            ok, detail = False, "异常：" + traceback.format_exc(limit=3).replace("\n", " | ")
        results.append((pid, title, ok, detail))
        print("[%s] %s %s —— %s" % ("PASS" if ok else "FAIL", pid, title, detail))

    passed = sum(1 for r in results if r[2])
    print("-" * 60)
    print("%s %d/%d" % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    if passed != len(results):
        print("失败项：" + ", ".join(r[0] for r in results if not r[2]))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
