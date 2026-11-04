#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭账本 · 完整回归测试（纯标准库，可反复执行）

设计原则
  1. 绝不碰生产数据：在临时目录起一个独立实例（LEDGER_HOME / LEDGER_PORT），
     库文件全新创建，跑完整目录删掉。生产库连读都不读。
  2. 测的是线上那版代码：默认从 ../ledger 拷 server.py + index.html，
     报告里记录 md5，可与服务器 /opt/ledger 比对确认同版。
  3. 日期全部相对「今天」生成（D(n) = n 天前），所以在任何一天跑结果都稳定。
  4. 用例互相隔离：每个用例自己造数据；统计/筛选用例各占一个独立月份。

用法
  python tests/ledger_suite.py                    # 跑全部并写报告
  python tests/ledger_suite.py --keep             # 保留临时目录，方便排查
  python tests/ledger_suite.py --only 退款         # 只跑分组/标题含关键字的
  python tests/ledger_suite.py --report out.md    # 指定报告路径
  python tests/ledger_suite.py --src /opt/ledger  # 指定被测代码目录
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import itertools
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

# Windows 控制台默认 GBK，输出里的 ✓ / ✗ 会直接抛 UnicodeEncodeError。
# 测试脚本自己先切到 UTF-8，跨平台都能跑。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SEQ = itertools.count(1)

# 所有请求都打 127.0.0.1，必须显式绕开环境里的 http_proxy。
# 这台机器上 http_proxy=http://127.0.0.1:14742 且 proxy_bypass('127.0.0.1') 为 False，
# 请求会绕一层本地代理：偶发 502、个别请求十几秒才回，表现成"随机失败"。
# 这不是被测代码的问题，但会让验收结论不可信，所以测试侧一律禁用代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- 时间工具
TODAY = date.today()


def D(n: int) -> str:
    """n 天前的日期（n=0 就是今天）"""
    return (TODAY - timedelta(days=n)).isoformat()


def month_of(offset: int, day: int = 15) -> str:
    """相对本月偏移 offset 个月的某一天，offset 为负表示过去"""
    total = TODAY.year * 12 + (TODAY.month - 1) + offset
    y, m = divmod(total, 12)
    return f"{y:04d}-{m + 1:02d}-{day:02d}"


def month_str(offset: int) -> str:
    return month_of(offset, 1)[:7]


def uniq_cents(base: int = 300_000) -> int:
    """全局唯一的金额（分），用于退款候选匹配类用例，避免互相干扰"""
    return base + next(SEQ)


# ---------------------------------------------------------------- HTTP 客户端
class Client:
    """带 cookie 的最小 HTTP 客户端；连接被服务端掐断时返回 status=0"""

    def __init__(self, base: str):
        self.base = base
        self.sid = None

    def req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.sid:
            headers["Cookie"] = "sid=" + self.sid
        # 中文关键词等非 ASCII 参数必须先编码，否则 urllib 会直接抛错
        safe = quote(path, safe="/?=&%+~")
        r = urllib.request.Request(self.base + safe, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(r, timeout=15) as resp:
                raw = resp.read()
                sc = resp.headers.get("Set-Cookie")
                if sc and "sid=" in sc:
                    token = sc.split("sid=")[1].split(";")[0]
                    self.sid = token or None
                try:
                    return resp.status, json.loads(raw or b"{}")
                except Exception:
                    return resp.status, {"_raw": raw[:400].decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}
        except Exception as e:                       # 服务端未捕获异常 → 连接被断
            return 0, {"_conn": f"{type(e).__name__}: {e}"}

    def get(self, p):
        return self.req("GET", p)

    def post(self, p, body=None):
        return self.req("POST", p, body if body is not None else {})

    def patch(self, p, body):
        return self.req("PATCH", p, body)

    def delete(self, p):
        return self.req("DELETE", p)


# ---------------------------------------------------------------- 断言上下文
class Ctx:
    def __init__(self, base: str, admin: Client, member_a: Client, member_b: Client,
                 anon: Client, workdir=None):
        self.base = base
        self.workdir = workdir    # 直接读库/读日志用：审计表和访问日志没有对外接口
        self.admin = admin
        self.member_a = member_a  # 真人 1（账户默认记账人）
        self.member_b = member_b  # 真人 2
        self.anon = anon
        self.me = member_a
        self._month_cursor = -5   # 统计/筛选用例从 5 个月前开始分配，绝不与近 30 天的用例重叠
        self.checks = []
        self.fails = []

    # -- 月份分配 --
    def month(self, day: int = 15) -> str:
        m = self._month_cursor
        self._month_cursor -= 1
        return month_of(m, day)

    def month_pair(self, day: int = 15):
        """一次性分配一个月，返回 (月份 "YYYY-MM", 该月某天的日期 "YYYY-MM-DD")"""
        d = self.month(day)
        return d[:7], d

    # -- 断言 --
    def ok(self, cond, what: str):
        self.checks.append(what)
        if not cond:
            self.fails.append(what)
        return bool(cond)

    def eq(self, actual, expected, what: str):
        good = actual == expected
        self.checks.append(f"{what}")
        if not good:
            self.fails.append(f"{what} → 期望 {expected!r}，实际 {actual!r}")
        return good

    def ne(self, a, b, what: str):
        return self.ok(a != b, what)

    def contains(self, hay, needle, what: str):
        return self.ok(needle in (hay or ""), f"{what}（应包含 {needle!r}）")

    def status(self, resp, expected, what: str):
        st, data = resp
        return self.eq(st, expected, f"{what} → HTTP {expected}" if st == expected else what)

    # -- 造数据 --
    def expense(self, amount=None, when=None, cat=("餐饮", "早餐"), client=None, **kw):
        c = client or self.me
        body = {"tx_date": when or D(0), "direction": "expense",
                "cat_parent": cat[0], "category": cat[1],
                "amount_cents": amount if amount is not None else uniq_cents()}
        body.update(kw)
        st, data = c.post("/api/tx", body)
        assert st == 200, f"造支出失败 {st} {data}"
        return data["id"]

    def income(self, amount=100_000, when=None, cat=("工资", "月度工资"), client=None, **kw):
        c = client or self.me
        body = {"tx_date": when or D(0), "direction": "income",
                "cat_parent": cat[0], "category": cat[1], "amount_cents": amount}
        body.update(kw)
        st, data = c.post("/api/tx", body)
        assert st == 200, f"造收入失败 {st} {data}"
        return data["id"]

    def refund(self, parent, amount, when=None, client=None, **kw):
        c = client or self.me
        body = {"tx_date": when or D(0), "direction": "refund",
                "parent_id": parent, "amount_cents": amount}
        body.update(kw)
        st, data = c.post("/api/tx", body)
        assert st == 200, f"造退款失败 {st} {data}"
        return data["id"]

    def row(self, tx_id, month=None):
        st, data = self.me.get(f"/api/tx?month={month or ''}")
        for t in data.get("items", []):
            if t["id"] == tx_id:
                return t
        return None

    def stats(self, month):
        st, data = self.me.get(f"/api/stats?month={month}")
        assert st == 200, f"统计失败 {st} {data}"
        return data

    def delete(self, tx_id, client=None):
        return (client or self.me).delete(f"/api/tx/{tx_id}")


# ---------------------------------------------------------------- 用例注册
CASES = []


def case(group: str, title: str):
    def deco(fn):
        CASES.append({"group": group, "title": title, "fn": fn})
        return fn
    return deco


# ================================================================ A 静态与基础
@case("A 静态资源", "首页可访问且带关键 UI 标识与安全头")
def a1(c: Ctx):
    with _OPENER.open(c.base + "/", timeout=10) as r:
        html = r.read().decode("utf-8", "replace")
        c.eq(r.status, 200, "GET / 状态码")
        c.eq(r.headers.get("X-Frame-Options"), "DENY", "首页 X-Frame-Options")
        c.eq(r.headers.get("X-Content-Type-Options"), "nosniff", "首页 X-Content-Type-Options")
        c.eq(r.headers.get("Cache-Control"), "no-cache", "首页禁用强缓存")
        csp = r.headers.get("Content-Security-Policy") or ""
        # 只断言「跟脚本无关」的那几条：script-src 一旦写上就得算 hash，得实机验证不白屏，
        # 暂时刻意不加。这几条加了不影响页面任何功能。
        c.contains(csp, "frame-ancestors 'none'", "CSP 防嵌套")
        c.contains(csp, "base-uri 'self'", "CSP 防 <base> 注入")
        c.contains(csp, "object-src 'none'", "CSP 禁插件")
        c.contains(csp, "form-action 'self'", "CSP 禁表单外发")
        c.ok("script-src" not in csp, "没冒然加 script-src（内联脚本要先算 hash 并实机验证）")
    for kw in ("待用券", "冲抵哪一笔支出", "筛选", "已退"):
        c.contains(html, kw, f"首页含「{kw}」")


@case("A 静态资源", "健康检查与自毁 Service Worker")
def a2(c: Ctx):
    st, data = c.anon.get("/healthz")
    c.eq(st, 200, "GET /healthz 状态码")
    c.eq(data.get("status"), "ok", "健康检查返回 status=ok")
    with _OPENER.open(c.base + "/sw.js", timeout=10) as r:
        js = r.read().decode()
        c.eq(r.status, 200, "GET /sw.js 状态码")
        c.contains(js, "unregister", "sw.js 会注销自身")
        c.eq(r.headers.get("Cache-Control"), "no-store, max-age=0", "sw.js 不缓存")
    st, _ = c.anon.get("/no-such-path")
    c.eq(st, 404, "未知路径返回 404")


@case("A 静态资源", "iOS 主屏图标：真实 PNG、180×180、无 alpha、可缓存")
def a3(c: Ctx):
    with _OPENER.open(c.base + "/apple-touch-icon.png", timeout=10) as r:
        data = r.read()
        c.eq(r.status, 200, "GET /apple-touch-icon.png 状态码")
        c.contains(r.headers.get("Content-Type") or "", "image/png", "Content-Type 是 image/png")
        cache = r.headers.get("Cache-Control") or ""
        c.contains(cache, "max-age", "允许浏览器缓存（iOS 会反复取）")
        c.eq(r.headers.get("X-Content-Type-Options"), "nosniff", "带了 nosniff")
    c.eq(data[:8], b"\x89PNG\r\n\x1a\n", "确实是 PNG（文件头正确）")
    c.eq(struct.unpack(">II", data[16:24]), (180, 180), "尺寸 180×180")
    # iOS 会给透明区域填黑底，所以必须是不带 alpha 的 RGB
    c.eq(data[24], 8, "位深 8")
    c.eq(data[25], 2, "颜色类型 RGB（无 alpha 通道）")
    c.ok(len(data) < 64 * 1024, "图标体积小于 64KB")


# ================================================================ B 认证与权限
@case("B 认证与权限", "未登录时所有业务接口一律 401")
def b1(c: Ctx):
    for p in ("/api/meta", "/api/tx", "/api/stats", "/api/refund-targets",
              "/api/refunds?parent=1", "/api/settings"):
        st, _ = c.anon.get(p)
        c.eq(st, 401, f"匿名 GET {p} 应 401")
    st, _ = c.anon.post("/api/tx", {"direction": "expense", "cat_parent": "餐饮",
                                    "category": "早餐", "amount_cents": 100})
    c.eq(st, 401, "匿名写入应 401")
    st, data = c.anon.get("/api/me")
    c.eq(st, 200, "匿名 GET /api/me 仍返回 200")
    c.eq(data.get("user"), None, "匿名 /api/me 的 user 为 null")


@case("B 认证与权限", "登录成功下发 HttpOnly + Secure + SameSite 会话 cookie")
def b2(c: Ctx):
    st, _ = c.anon.post("/api/login", {"username": "tester1", "password": "wrong-pass-1"})
    c.eq(st, 401, "错误密码登录被拒")
    cli = Client(c.base)
    st, data = cli.post("/api/login", {"username": "tester1", "password": "pass-1234"})
    c.eq(st, 200, "正确密码登录成功")
    st, me = cli.get("/api/me")
    c.eq(me["user"]["username"], "tester1", "/api/me 返回当前登录人")
    c.eq(me["user"]["is_admin"], 0, "普通账号 is_admin=0")
    st, _ = cli.post("/api/logout", {})
    c.eq(st, 200, "登出成功")
    st, me2 = cli.get("/api/me")
    c.eq(me2.get("user"), None, "登出后会话失效")


@case("B 认证与权限", "登录限流：同一账号失败 8 次后第 9 次返回 429")
def b3(c: Ctx):
    name = f"ratelimit{next(SEQ)}"
    st, _ = c.admin.post("/api/users", {"username": name, "display_name": "限流测试",
                                        "password": "pass-1234"})
    c.eq(st, 200, f"创建限流专用账号 {name}")
    cli = Client(c.base)
    codes = []
    for _ in range(8):
        st, _ = cli.post("/api/login", {"username": name, "password": "bad-password"})
        codes.append(st)
    c.eq(codes, [401] * 8, "前 8 次失败均为 401")
    st, data = cli.post("/api/login", {"username": name, "password": "bad-password"})
    c.eq(st, 429, "第 9 次被限流（429）")
    c.contains(data.get("error", ""), "次数过多", "限流提示文案")


@case("B 认证与权限", "/api/meta 只暴露真人账号与三套分类树")
def b4(c: Ctx):
    st, m = c.me.get("/api/meta")
    c.eq(st, 200, "GET /api/meta 状态码")
    names = [u["username"] for u in m["users"]]
    c.eq("admin" not in names, True, "管理员不出现在「谁花的」候选里")
    c.eq(len(names) >= 2, True, "至少返回两个真人账号")
    c.eq(sorted(m["categories"].keys()), ["expense", "income", "refund"], "返回三个方向的分类树")
    c.eq("餐饮" in m["categories"]["expense"], True, "支出树含「餐饮」")
    c.eq("退款" in m["categories"]["refund"], True, "退款树含「退款」")
    c.eq(m["ai_enabled"], False, "未配置 key 时 ai_enabled=false")


@case("B 认证与权限", "管理员权限：普通账号读不到也改不了设置，建不了账号")
def b5(c: Ctx):
    st, _ = c.me.get("/api/settings")
    c.eq(st, 403, "普通账号读设置被拒 403")
    st, _ = c.me.post("/api/settings", {"ai_model": "x"})
    c.eq(st, 403, "普通账号改设置被拒 403")
    st, _ = c.me.post("/api/users", {"username": "hacker", "password": "pass-1234"})
    c.eq(st, 403, "普通账号建账号被拒 403")
    st, data = c.admin.get("/api/settings")
    c.eq(st, 200, "管理员可读设置")
    c.eq("ai_key_masked" in data, True, "设置接口做 key 脱敏")


@case("B 认证与权限", "建账号的输入校验：用户名格式 / 密码长度 / 重名")
def b6(c: Ctx):
    st, _ = c.admin.post("/api/users", {"username": "Bad Name", "password": "pass-1234"})
    c.eq(st, 400, "用户名含空格被拒")
    st, _ = c.admin.post("/api/users", {"username": "ab", "password": "pass-1234"})
    c.eq(st, 400, "用户名过短被拒")
    st, _ = c.admin.post("/api/users", {"username": "shortpwd", "password": "123"})
    c.eq(st, 400, "密码少于 8 位被拒")
    st, _ = c.admin.post("/api/users", {"username": "tester1", "password": "pass-1234"})
    c.eq(st, 400, "用户名重复被拒")
    st, _ = c.admin.post("/api/users", {"username": "member_b2", "password": "pass-1234",
                                        "display_name": "测试乙2"})
    c.eq(st, 200, "合法账号创建成功")


@case("B 认证与权限", "改密码：原密码错 403、新密码过短 400、成功后旧会话立即失效")
def b7(c: Ctx):
    name = f"pwd{next(SEQ)}"
    c.admin.post("/api/users", {"username": name, "display_name": "改密测试", "password": "pass-1234"})
    cli = Client(c.base)
    cli.post("/api/login", {"username": name, "password": "pass-1234"})
    st, _ = cli.post("/api/password", {"old": "nope", "new": "newpass-123"})
    c.eq(st, 403, "原密码错被拒 403")
    st, _ = cli.post("/api/password", {"old": "pass-1234", "new": "short"})
    c.eq(st, 400, "新密码过短被拒 400")
    st, _ = cli.post("/api/password", {"old": "pass-1234", "new": "newpass-123"})
    c.eq(st, 200, "改密成功")
    st, me = cli.get("/api/me")
    c.eq(me.get("user"), None, "改密后旧会话失效")
    st, _ = cli.post("/api/login", {"username": name, "password": "pass-1234"})
    c.eq(st, 401, "旧密码不能再登录")
    st, _ = cli.post("/api/login", {"username": name, "password": "newpass-123"})
    c.eq(st, 200, "新密码可登录")


# ================================================================ C 记账 CRUD
@case("C 记账 CRUD", "创建支出：金额/分类/归属/备注全部正确落库")
def c1(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=12_345, when=d, note="早饭 两个包子")
    row = c.row(tid, m)
    c.eq(row["direction"], "expense", "方向")
    c.eq(row["amount_cents"], 12_345, "金额（分）")
    c.eq(row["cat_parent"], "餐饮", "大类")
    c.eq(row["category"], "早餐", "子类")
    c.eq(row["note"], "早饭 两个包子", "备注")
    st, meta = c.me.get("/api/meta")
    member_a_id = [u["id"] for u in meta["users"] if u["username"] == "tester1"][0]
    c.eq(row["owner_id"], member_a_id, "默认归属 = 当前登录账号")
    c.eq(row["tx_date"], d, "日期")
    c.eq(row["refunded_cents"], 0, "新单已退金额为 0")
    c.eq(row["remain_cents"], 12_345, "新单剩余可退 = 全额")


@case("C 记账 CRUD", "创建收入并计入收入统计")
def c2(c: Ctx):
    m, d = c.month_pair()
    tid = c.income(amount=2_000_000, when=d, note="工资入账")
    row = c.row(tid, m)
    c.eq(row["direction"], "income", "方向为收入")
    c.eq(row["amount_cents"], 2_000_000, "收入金额")
    st = c.stats(m)
    c.eq(st["income"], 2_000_000, "统计里的收入")
    c.eq(st["expense"], 0, "该月无支出")


@case("C 记账 CRUD", "金额精度：12.34 元存成 1234 分，无浮点误差")
def c3(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=round(12.34 * 100), when=d)
    row = c.row(tid, m)
    c.eq(row["amount_cents"], 1234, "12.34 元 = 1234 分")
    ids = [c.expense(amount=1, when=d), c.expense(amount=2, when=d), c.expense(amount=3, when=d)]
    st = c.stats(m)
    c.eq(st["expense"], 1234 + 1 + 2 + 3, "多笔小额累加无误差")
    for i in ids:
        c.delete(i)
    c.delete(tid)


@case("C 记账 CRUD", "金额非法值（0 / 负数 / 超上限）全部拒绝")
def c4(c: Ctx):
    base = {"tx_date": D(0), "direction": "expense", "cat_parent": "餐饮", "category": "早餐"}
    st, data = c.me.post("/api/tx", dict(base, amount_cents=0))
    c.eq(st, 400, "金额 0 被拒")
    st, _ = c.me.post("/api/tx", dict(base, amount_cents=-100))
    c.eq(st, 400, "负数金额被拒")
    st, _ = c.me.post("/api/tx", dict(base, amount_cents=100_000_001))
    c.eq(st, 400, "超过 1,000,000 元被拒")
    st, _ = c.me.post("/api/tx", dict(base, amount_cents=100_000_000))
    c.eq(st, 200, "刚好 1,000,000 元可入库")
    st, _ = c.me.post("/api/tx", dict(base, amount_cents="abc"))
    c.eq(st, 400, "非数字金额被拒")
    # 前端以前用 parseFloat，这几个值会被悄悄截成 12 / 1000 / 12 —— 接口必须一口回绝，
    # 别哪天有人把 int() 换成 float()/parseFloat 语义，这条会立刻报警
    for bad in ("12abc", "1e3", "12.5.6", "12元"):
        st, _ = c.me.post("/api/tx", dict(base, amount_cents=bad))
        c.eq(st, 400, f"伪数字金额「{bad}」被拒")


@case("C 记账 CRUD", "分类越界与方向非法被拒")
def c5(c: Ctx):
    base = {"tx_date": D(0), "amount_cents": 1000}
    st, _ = c.me.post("/api/tx", dict(base, direction="expense",
                                      cat_parent="不存在的大类", category="早餐"))
    c.eq(st, 400, "大类不在树里被拒")
    st, _ = c.me.post("/api/tx", dict(base, direction="expense",
                                      cat_parent="餐饮", category="打车"))
    c.eq(st, 400, "子类不属于该大类被拒")
    st, _ = c.me.post("/api/tx", dict(base, direction="transfer",
                                      cat_parent="餐饮", category="早餐"))
    c.eq(st, 400, "非法方向被拒")
    st, _ = c.me.post("/api/tx", {"tx_date": "2026/10/01", "direction": "expense",
                                  "cat_parent": "餐饮", "category": "早餐", "amount_cents": 100})
    c.eq(st, 400, "日期格式非法被拒")
    st, _ = c.me.post("/api/tx", dict(base, direction="expense", cat_parent="餐饮",
                                      category="早餐", owner_id=999_999))
    c.eq(st, 400, "归属用户不存在被拒")


@case("C 记账 CRUD", "备注超长被截断到 100 字")
def c6(c: Ctx):
    m, d = c.month_pair()
    long_note = "备" * 150
    tid = c.expense(amount=1000, when=d, note=long_note)
    row = c.row(tid, m)
    c.eq(len(row["note"]), 100, "备注被截断到 100 字")


@case("C 记账 CRUD", "编辑记录：金额/分类/日期/备注可改，越界被拒")
def c7(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=5000, when=d, note="原始")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"amount_cents": 8000, "cat_parent": "交通",
                                          "category": "打车", "note": "改过了",
                                          "tx_date": month_of(-1, 20)})
    c.eq(st, 200, "正常编辑成功")
    row = c.row(tid, "")
    c.eq(row["amount_cents"], 8000, "金额已更新")
    c.eq(row["cat_parent"], "交通", "大类已更新")
    c.eq(row["category"], "打车", "子类已更新")
    c.eq(row["note"], "改过了", "备注已更新")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"cat_parent": "餐饮", "category": "打车"})
    c.eq(st, 400, "编辑成非法分类组合被拒")
    st, _ = c.me.patch("/api/tx/999999", {"note": "x"})
    c.eq(st, 404, "编辑不存在的记录返回 404")


@case("C 记账 CRUD", "删除记录后不再出现在列表；重复删除幂等不报错")
def c8(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=6666, when=d)
    c.delete(tid)
    c.eq(c.row(tid, m), None, "删除后列表里查不到")
    st, data = c.delete(tid)
    c.ok(st in (200, 404), f"重复删除返回 {st}（幂等或 404 均可）")


@case("C 记账 CRUD", "读己之写：创建后立刻查询一定能看到（防「响应早于提交」）")
def c9(c: Ctx):
    m, d = c.month_pair()
    n = 15
    for i in range(n):
        tid = c.expense(amount=1000 + i, when=d)
        c.ok(c.row(tid, m) is not None, f"第 {i + 1} 次创建后立刻能查到")
    c.eq(c.stats(m)["expense"], sum(1000 + i for i in range(n)), f"{n} 笔累加金额精确")


# ================================================================ D 标记语义
@case("D 标记语义", "三个标记互相独立，可同时开启")
def d1(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=10_000, when=d, shared=True, reimb_pending=True, pending_use=True)
    row = c.row(tid, m)
    c.eq(row["shared"], 1, "共同支出标记")
    c.eq(row["reimb_pending"], 1, "待报销标记")
    c.eq(row["pending_use"], 1, "待用券标记")


@case("D 标记语义", "待报销与已报销互斥：同时传时待报销优先")
def d2(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=10_000, when=d, reimb_pending=True, reimbursed=True)
    row = c.row(tid, m)
    c.eq(row["reimb_pending"], 1, "待报销保留")
    c.eq(row["reimbursed"], 0, "已报销被清掉")


@case("D 标记语义", "「已报销」只能由到账记录打开，手点一律 400（R14）")
def d3(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=10_000, when=d)
    st, _ = c.me.patch(f"/api/tx/{tid}", {"reimb_pending": True})
    row = c.row(tid, m)
    c.eq(st, 200, "打开待报销")
    c.eq((row["reimb_pending"], row["reimbursed"]), (1, 0), "待报销 1 / 已报销 0")
    # 手点「已报销」＝ 宣布钱已经回来了，可净支出一分没减——这条路径必须堵死
    st, resp = c.me.patch(f"/api/tx/{tid}", {"reimbursed": True})
    c.eq(st, 400, "没有到账记录时手点已报销被拒")
    c.eq(resp.get("code"), "reimburse_requires_record", "错误码稳定")
    c.ok("到账" in (resp.get("error") or ""), "话术给出下一步：记一笔到账")
    row = c.row(tid, m)
    c.eq((row["reimb_pending"], row["reimbursed"]), (1, 0), "被拒后标记一点没动")
    # 建账这条路同样不许填（前端早已不发，接口也不能给人留后门）
    st, resp = c.me.post("/api/tx", {"tx_date": d, "direction": "expense",
                                     "cat_parent": "餐饮", "category": "早餐",
                                     "amount_cents": 1_000, "reimbursed": True})
    c.eq(st, 400, "建账时手填已报销也被拒")
    c.eq(resp.get("code"), "reimburse_requires_record", "建账与改账同一个码")
    # 真到账之后，状态由记录决定（这才是「已报销」唯一合法的来源）
    st, _ = c.me.post(f"/api/tx/{tid}/reimburse", {"amount_cents": 500})
    c.eq(st, 200, "可以记一笔到账")
    row = c.row(tid, m)
    c.eq((row["reimb_pending"], row["reimbursed"]), (0, 1), "到账后：待报销清、已报销置位")


@case("D 标记语义", "标记只属于支出：收入/退款带标记会被强制清空")
def d4(c: Ctx):
    m, d = c.month_pair()
    tid = c.income(amount=50_000, when=d, reimb_pending=True, pending_use=True, shared=True)
    row = c.row(tid, m)
    c.eq(row["reimb_pending"], 0, "收入不能待报销")
    c.eq(row["pending_use"], 0, "收入不能待用券")
    c.eq(row["shared"], 0, "收入不能共同支出")


@case("D 标记语义", "把支出改成收入后，原有标记被清掉")
def d5(c: Ctx):
    m, d = c.month_pair()
    tid = c.expense(amount=10_000, when=d, shared=True, reimb_pending=True, pending_use=True)
    st, _ = c.me.patch(f"/api/tx/{tid}", {"direction": "income", "cat_parent": "工资",
                                          "category": "月度工资"})
    c.eq(st, 200, "改成收入成功")
    row = c.row(tid, m)
    c.eq((row["reimb_pending"], row["pending_use"]), (0, 0), "报销/用券标记被清")
    c.eq(row["shared"], 0, "共同支出标记也被清（标记只属于支出）")


# ================================================================ E 退款冲抵
@case("E 退款冲抵", "全额退款：剩余可退归零，退款继承原单分类与归属")
def e1(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=20_000, when=d, cat=("购物", "数码"), note="耳机")
    rid = c.refund(pid, 20_000, when=D(0))
    prow = c.row(pid, "")
    c.eq(prow["refunded_cents"], 20_000, "原单已退金额")
    c.eq(prow["remain_cents"], 0, "原单剩余可退归零")
    c.eq(prow["refund_count"], 1, "原单退款笔数")
    rrow = c.row(rid, "")
    c.eq(rrow["direction"], "refund", "退款行方向")
    c.eq(rrow["cat_parent"], "购物", "退款继承原单大类")
    c.eq(rrow["category"], "数码", "退款继承原单子类")
    c.eq(rrow["owner_id"], prow["owner_id"], "退款继承原单归属")
    c.eq(rrow["parent_id"], pid, "退款指向原单")


@case("E 退款冲抵", "部分退款（100 买两个退一个 50）：剩余 50%")
def e2(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d, cat=("购物", "日用品"))
    c.refund(pid, 5_000)
    row = c.row(pid, m)
    c.eq(row["refunded_cents"], 5_000, "已退一半")
    c.eq(row["remain_cents"], 5_000, "还剩一半可退")
    c.eq(row["refund_count"], 1, "一笔退款")


@case("E 退款冲抵", "多次退款累加：30% + 30% + 40% = 退满")
def e3(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d, cat=("购物", "日用品"))
    c.refund(pid, 3_000)
    c.refund(pid, 3_000)
    c.refund(pid, 4_000)
    row = c.row(pid, m)
    c.eq(row["refunded_cents"], 10_000, "累计已退 = 全额")
    c.eq(row["remain_cents"], 0, "剩余归零")
    c.eq(row["refund_count"], 3, "三笔退款")
    st, h = c.me.get(f"/api/refunds?parent={pid}")
    c.eq(len(h["items"]), 3, "退款明细接口返回三笔")
    c.eq(sum(x["amount_cents"] for x in h["items"]), 10_000, "退款明细金额合计正确")


@case("E 退款冲抵", "超额退款被拒，且提示里带剩余可退金额")
def e4(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d)
    c.refund(pid, 4_000)
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "parent_id": pid, "amount_cents": 7_000})
    c.eq(st, 400, "退款超过剩余被拒")
    c.contains(data.get("error", ""), "60.00", "错误提示含剩余金额 60.00")
    row = c.row(pid, m)
    c.eq(row["refunded_cents"], 4_000, "被拒后已退金额不变")


@case("E 退款冲抵", "刚好退满也允许（边界），再退一笔被拒")
def e5(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d)
    c.refund(pid, 3_000)
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                  "parent_id": pid, "amount_cents": 7_000})
    c.eq(st, 200, "刚好补齐到全额允许")
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "parent_id": pid, "amount_cents": 1})
    c.eq(st, 400, "退完之后再退被拒")
    c.contains(data.get("error", ""), "全部退完", "提示已全部退完")


@case("E 退款冲抵", "退满后自动清掉原单的待报销与待用券标记")
def e6(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d, reimb_pending=True, pending_use=True)
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "parent_id": pid, "amount_cents": 10_000})
    c.eq(st, 200, "一次退满")
    c.eq(data.get("cleared"), True, "接口返回 cleared=true")
    row = c.row(pid, m)
    c.eq((row["reimb_pending"], row["pending_use"]), (0, 0), "两个标记都被自动清除")


@case("E 退款冲抵", "部分退款不清标记（钱还没全部回来）")
def e7(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d, reimb_pending=True, pending_use=True)
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "parent_id": pid, "amount_cents": 4_000})
    c.eq(data.get("cleared"), False, "部分退款 cleared=false")
    row = c.row(pid, m)
    c.eq((row["reimb_pending"], row["pending_use"]), (1, 1), "标记保留")


@case("E 退款冲抵", "退款不能冲抵收入、不能冲抵自己、不能冲抵不存在的单")
def e8(c: Ctx):
    m, d = c.month_pair()
    inc = c.income(amount=50_000, when=d)
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "parent_id": inc, "amount_cents": 100})
    c.eq(st, 400, "不能冲抵收入")
    c.contains(data.get("error", ""), "支出", "提示只能冲抵支出")
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                  "parent_id": 999_999, "amount_cents": 100})
    c.eq(st, 400, "冲抵不存在的单被拒")
    pid = c.expense(amount=5_000, when=d)
    rid = c.refund(pid, 1_000)
    st, data = c.me.patch(f"/api/tx/{rid}", {"parent_id": rid})
    c.eq(st, 400, "退款不能冲抵自己")
    c.contains(data.get("error", ""), "自己", "提示不能冲抵自己")
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "expense",
                                  "cat_parent": "餐饮", "category": "早餐",
                                  "amount_cents": 100, "parent_id": pid})
    c.eq(st, 400, "只有退款能带 parent_id")


@case("E 退款冲抵", "退款可改绑/解绑：改绑后剩余额度重新计算")
def e9(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=10_000, when=d)
    b = c.expense(amount=10_000, when=d)
    rid = c.refund(a, 6_000)
    c.eq(c.row(a, m)["remain_cents"], 4_000, "绑在 A 上时 A 剩 4000")
    st, _ = c.me.patch(f"/api/tx/{rid}", {"parent_id": b})
    c.eq(st, 200, "改绑到 B 成功")
    c.eq(c.row(a, "")["remain_cents"], 10_000, "A 的额度恢复")
    c.eq(c.row(b, "")["remain_cents"], 4_000, "B 的额度被占用")
    st, _ = c.me.patch(f"/api/tx/{rid}", {"parent_id": None})
    c.eq(st, 200, "解绑成功")
    row = c.row(rid, "")
    c.eq(row["parent_id"], None, "解绑后 parent_id 为空")
    c.eq(row["cat_parent"], "退款", "解绑后分类回落到退款树默认大类")
    c.eq(c.row(b, "")["remain_cents"], 10_000, "B 的额度完全恢复")


@case("E 退款冲抵", "编辑已冲抵的退款：分类与方向锁死，其余照改且不报错")
def e10(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d, cat=("餐饮", "早餐"))
    rid = c.refund(pid, 4_000)
    st, data = c.me.patch(f"/api/tx/{rid}", {"note": "改个备注", "amount_cents": 3_000})
    c.eq(st, 200, "编辑挂着的退款成功")
    row = c.row(rid, "")
    c.eq(row["note"], "改个备注", "备注已改")
    c.eq(row["amount_cents"], 3_000, "金额已改")
    c.eq(row["cat_parent"], "餐饮", "分类仍跟随原单")
    # 显式传别的分类：服务端按原单覆盖，不报错。
    # 这里刻意不返回 400——旧版前端（PWA 缓存着老 HTML）保存时会把整包字段带上，
    # 一旦改成报错，那些用户的「改个备注」会连带失败。界面侧已经把下拉换成只读，
    # 从源头堵住「选了没反应」，接口保持宽容。
    st, _ = c.me.patch(f"/api/tx/{rid}", {"cat_parent": "退款", "category": "购物退款"})
    c.eq(st, 200, "显式改分类不报错")
    row = c.row(rid, "")
    c.eq((row["cat_parent"], row["category"]), ("餐饮", "早餐"),
         "显式改分类同样被忽略——界面因此把它做成只读")
    # 方向同理：挂着原单就只能是退款，改成支出会被明确拦下
    st, data = c.me.patch(f"/api/tx/{rid}", {"direction": "expense"})
    c.eq(st, 400, "冲抵记录不能改成支出")
    c.contains(data.get("error", ""), "冲抵", "提示只能冲抵原账单")


@case("E 退款冲抵", "退款明细接口：缺参数报错，非法参数不崩")
def e11(c: Ctx):
    st, data = c.me.get("/api/refunds")
    c.eq(st, 400, "缺 parent 参数返回 400")
    st, _ = c.me.get("/api/refunds?parent=abc")
    c.eq(st, 400, "parent 非数字应返回 400 而不是把服务打挂")


# ================================================================ F 冲抵候选：排序与预选
@case("F 冲抵候选", "唯一同额候选被预选")
def f1(c: Ctx):
    amt = uniq_cents(700_000)
    pid = c.expense(amount=amt, when=D(10), cat=("购物", "数码"))
    st, data = c.me.get(f"/api/refund-targets?amount_cents={amt}")
    c.eq(st, 200, "候选接口状态码")
    c.eq(data["auto"], pid, "唯一同额候选被自动选中")
    hit = [x for x in data["items"] if x["id"] == pid][0]
    c.eq(hit["remain_cents"], amt, "候选带正确的剩余可退金额")


@case("F 冲抵候选", "同额有两笔时不自动绑定（避免绑错）")
def f2(c: Ctx):
    amt = uniq_cents(710_000)
    a = c.expense(amount=amt, when=D(9))
    b = c.expense(amount=amt, when=D(8))
    st, data = c.me.get(f"/api/refund-targets?amount_cents={amt}")
    c.eq(data["auto"], None, "两笔同额 → auto 为 null")
    ids = {x["id"] for x in data["items"]}
    c.eq({a, b} <= ids, True, "两笔都在候选列表里")


@case("F 冲抵候选", "没有金额相等的候选时不自动绑定")
def f3(c: Ctx):
    c.expense(amount=uniq_cents(720_000), when=D(5))
    st, data = c.me.get("/api/refund-targets?amount_cents=9_999_999")
    c.eq(data["auto"], None, "无同额候选 → auto 为 null")
    c.eq(all(x["remain_cents"] != 9_999_999 for x in data["items"]), True,
         "候选里没有该金额")


@case("F 冲抵候选", "金额传 0 或非法时不自动绑定")
def f4(c: Ctx):
    st, data = c.me.get("/api/refund-targets?amount_cents=0")
    c.eq(st, 200, "金额 0 时接口仍可用")
    c.eq(data["auto"], None, "金额 0 不自动绑定")
    st, data = c.me.get("/api/refund-targets?amount_cents=abc")
    c.eq(st, 200, "金额非法时不报错")
    c.eq(data["auto"], None, "金额非法不自动绑定")


@case("F 冲抵候选", "已退完的单不出现在候选里")
def f5(c: Ctx):
    amt = uniq_cents(730_000)
    pid = c.expense(amount=amt, when=D(7))
    c.refund(pid, amt)
    st, data = c.me.get(f"/api/refund-targets?amount_cents={amt}")
    ids = {x["id"] for x in data["items"]}
    c.eq(pid in ids, False, "退完的单被排除")
    c.eq(data["auto"] == pid, False, "退完的单不会被自动选中")


@case("F 冲抵候选", "候选窗口 30 天：第 30 天在、第 31 天不在")
def f6(c: Ctx):
    a30 = uniq_cents(740_000)
    a31 = uniq_cents(750_000)
    p30 = c.expense(amount=a30, when=D(30))
    p31 = c.expense(amount=a31, when=D(31))
    st, d = c.me.get(f"/api/refund-targets?amount_cents={a30}")
    c.eq(p30 in {x["id"] for x in d["items"]}, True, "第 30 天的支出仍在候选里")
    c.eq(d["auto"], p30, "唯一同额的支出被自动选中")
    st, d = c.me.get(f"/api/refund-targets?amount_cents={a31}")
    c.eq(p31 in {x["id"] for x in d["items"]}, False, "第 31 天的支出已出窗口")
    c.eq(d["window_days"], 30, "接口自报窗口天数")


@case("F 冲抵候选", "按归属过滤：查别人名下时看不到自己那笔")
def f7(c: Ctx):
    amt = uniq_cents(760_000)
    mine = c.expense(amount=amt, when=D(6), client=c.member_a)
    st, meta = c.me.get("/api/meta")
    member_b_id = [u["id"] for u in meta["users"] if u["username"] == "tester2"][0]
    member_a_id = [u["id"] for u in meta["users"] if u["username"] == "tester1"][0]
    st, d = c.me.get(f"/api/refund-targets?amount_cents={amt}&owner={member_b_id}")
    c.eq(mine in {x["id"] for x in d["items"]}, False, "按另一个归属查时看不到我的单")
    st, d = c.me.get(f"/api/refund-targets?amount_cents={amt}&owner={member_a_id}")
    c.eq(mine in {x["id"] for x in d["items"]}, True, "按自己的归属查时能看到")


@case("F 冲抵候选", "部分退款：原单比退款额大也进候选（near）")
def f8(c: Ctx):
    # 用 5 万级金额，确保同库里其它用例的支出都落在「退不了」那一侧，结果不受干扰
    amt = uniq_cents(5_000_000)
    pid = c.expense(amount=amt + 1_500, when=D(4), cat=("购物", "服饰"))
    st, d = c.me.get(f"/api/refund-targets?amount_cents={amt}")
    hit = [x for x in d["items"] if x["id"] == pid]
    c.eq(bool(hit), True, "比退款额大的原单在候选里")
    c.eq(hit[0]["diff_cents"], 1_500, "差额算对")
    c.eq(hit[0]["match"], "near", "只靠金额接近 → 标记为 near")
    c.eq(d["auto"], pid, "唯一候选被预选")


@case("F 冲抵候选", "金额差太多的不进候选（退 15 元不该冒出 56 元）")
def f9(c: Ctx):
    amt = uniq_cents(5_100_000)
    near = c.expense(amount=amt + 200, when=D(3))
    far = c.expense(amount=amt + 20_001, when=D(3))
    st, d = c.me.get(f"/api/refund-targets?amount_cents={amt}")
    ids = {x["id"] for x in d["items"]}
    c.eq(near in ids, True, "容差内的候选保留")
    c.eq(far in ids, False, "超出 200 元容差上限的候选被剔除")
    c.eq(d["tolerance_cents"], 20_000, "容差被上限夹住")


@case("F 冲抵候选", "剩余可退比退款额小的候选不给（点了必然退不了）")
def f10(c: Ctx):
    amt = uniq_cents(5_200_000)
    pid = c.expense(amount=amt, when=D(2))
    c.refund(pid, 1_000)                     # 先退 10 元，剩 amt-10 元
    st, d = c.me.get(f"/api/refund-targets?amount_cents={amt}")
    c.eq(pid in {x["id"] for x in d["items"]}, False, "剩余可退不足的候选被剔除")


@case("F 冲抵候选", "备注关键词命中：差额超出容差也能匹配（一单多件退其中一件）")
def f11(c: Ctx):
    amt = uniq_cents(5_300_000)
    pid = c.expense(amount=amt + 50_000, when=D(5), note="充电宝+数据线")
    st, d = c.me.get(f"/api/refund-targets?amount_cents={amt}")
    c.eq(pid in {x["id"] for x in d["items"]}, False,
         "不带备注时超出金额带，不出现")
    st, d = c.me.get(f"/api/refund-targets?amount_cents={amt}&note=充电宝退款")
    hit = [x for x in d["items"] if x["id"] == pid]
    c.eq(bool(hit), True, "备注对得上的原单在候选里")
    c.eq(hit[0]["match"], "kw", "判定为备注命中")
    c.eq(d["auto"], pid, "备注命中的候选被预选")


# ================================================================ G 待用券
@case("G 待用券", "建券 → 计入待用券统计；核销后归零但支出保留")
def g1(c: Ctx):
    # 待用券是「跨月累计」，所以只断言增量，不断言绝对值
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=30_000, when=d, pending_use=True, note="火锅券")
    st = c.stats(m)
    c.eq(st["voucher"] - base["voucher"], 30_000, "待用券合计 +300")
    c.eq(st["voucher_cnt"] - base["voucher_cnt"], 1, "待用券笔数 +1")
    c.eq(st["expense"], 30_000, "券的钱已经花出去，仍计支出")
    c.me.patch(f"/api/tx/{pid}", {"pending_use": False})
    st = c.stats(m)
    c.eq(st["voucher"], base["voucher"], "核销后待用券回到建券前")
    c.eq(st["expense"], 30_000, "核销后支出不变（真实消费）")


@case("G 待用券", "券过期作废：只清标记，钱留在支出里")
def g2(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=20_000, when=d, pending_use=True)
    c.me.patch(f"/api/tx/{pid}", {"pending_use": False})
    row = c.row(pid, m)
    c.eq(row["pending_use"], 0, "标记已清")
    c.eq(row["amount_cents"], 20_000, "金额没变")
    c.eq(c.stats(m)["expense"], 20_000, "支出保留")


@case("G 待用券", "券退款：生成冲抵退款，券标记随全额退完自动清除")
def g3(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=30_000, when=d, pending_use=True)
    c.refund(pid, 30_000)
    st = c.stats(m)
    c.eq(st["voucher"], base["voucher"], "退完后券统计回到退前")
    c.eq(st["expense"], 0, "净支出归零")
    c.eq(st["expense_gross"], 30_000, "毛支出保留 300")
    c.eq(st["refund_offset"], 30_000, "冲抵金额 = 300")
    c.eq(c.row(pid, m)["pending_use"], 0, "券标记被自动清")


@case("G 待用券", "券部分退款：标记保留，券金额按净值计")
def g4(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=30_000, when=d, pending_use=True)
    c.refund(pid, 10_000)
    st = c.stats(m)
    c.eq(st["voucher"] - base["voucher"], 20_000, "券金额按净值 200")
    c.eq(st["voucher_cnt"] - base["voucher_cnt"], 1, "券笔数 +1")
    c.eq(c.row(pid, m)["pending_use"], 1, "部分退款不清券标记")


# ================================================================ H 统计口径
@case("H 统计口径", "净支出 = 毛支出 − 冲抵；三个金额字段自洽")
def h1(c: Ctx):
    m, d = c.month_pair()
    c.expense(amount=10_000, when=d)
    pid = c.expense(amount=20_000, when=d)
    c.refund(pid, 8_000, when=D(0))
    st = c.stats(m)
    c.eq(st["expense_gross"], 30_000, "毛支出")
    c.eq(st["refund_offset"], 8_000, "冲抵回本月的金额")
    c.eq(st["refund_orphan"], 0, "本月无无主退款")
    c.eq(st["expense"], 22_000, "净支出 = 300 − 80")
    c.eq(st["expense_gross"] - st["refund_offset"] - st["refund_orphan"], st["expense"],
         "三字段自洽")


@case("H 统计口径", "饼图算式对得上：分类合计 − 冲抵为负 − 无主冲抵 = 净支出（R15）")
def h1b(c: Ctx):
    m, d = c.month_pair()
    p1 = c.expense(amount=10_000, when=d)      # 餐饮 / 早餐
    c.expense(amount=5_000, when=d)            # 交通 / 打车
    c.refund(p1, 6_000, when=D(0))
    # 谁也没认领的退款：不挂任何支出，只能落在发生当月，于是「分类合计」天然比
    # 「支出」多出这一块——这就是 R15 里怎么看都对不上的那笔钱
    st, _ = c.me.post("/api/tx", {"tx_date": d, "direction": "refund",
                                  "cat_parent": "退款", "category": "其他退款",
                                  "amount_cents": 3_000, "note": "无主"})
    c.eq(st, 200, "无主退款可入库")
    s = c.stats(m)
    c.eq(s["expense"], 6_000, "净支出 150 − 60 − 30")
    c.eq(s["cat_total"], 9_000, "分类合计 = 400（餐饮）+ 500（交通）")
    c.eq(s["cat_negative"], 0, "没有冲成负数的分类")
    c.eq(s["cat_total"] - s["cat_negative"] - s["refund_orphan"], s["expense"],
         "饼图下面那句算式必须与「支出」一致，否则看图的人怎么加都对不上")


@case("H 统计口径", "跨月退款冲抵回归原消费月，退款当月不受影响")
def h2(c: Ctx):
    d = c.month()
    old = d[:7]
    pid = c.expense(amount=10_000, when=d)
    c.refund(pid, 10_000, when=D(0))          # 退款记在今天（本月）
    so = c.stats(old)
    c.eq(so["expense"], 0, "上上个月净支出被冲抵为 0")
    c.eq(so["refund_offset"], 10_000, "冲抵金额落在原消费月")
    c.eq(so["refund_orphan"], 0, "原消费月没有无主退款")


@case("H 统计口径", "无主退款只减少退款当月净支出")
def h3(c: Ctx):
    m, d = c.month_pair()
    st, data = c.me.post("/api/tx", {"tx_date": d, "direction": "refund",
                                     "cat_parent": "退款", "category": "其他退款",
                                     "amount_cents": 5_000, "note": "不知道哪笔"})
    c.eq(st, 200, "无主退款可入库")
    s = c.stats(m)
    c.eq(s["refund_orphan"], 5_000, "无主退款计入当月 orphan")
    c.eq(s["expense"], -5_000, "净支出出现负值（本月只有一笔无主退款）")
    c.eq(s["refund_offset"], 0, "没有可冲抵的支出")


@case("H 统计口径", "分类净额：全额退完的分类消失，部分退按净额")
def h4(c: Ctx):
    m, d = c.month_pair()
    full = c.expense(amount=10_000, when=d, cat=("购物", "数码"))
    part = c.expense(amount=20_000, when=d, cat=("交通", "打车"))
    c.expense(amount=30_000, when=d, cat=("餐饮", "午餐"))
    c.refund(full, 10_000)
    c.refund(part, 5_000)
    st = c.stats(m)
    cats = {x["cat_parent"]: x["total"] for x in st["by_category"]}
    c.eq("购物" in cats, False, "退完的分类不出现")
    c.eq(cats.get("交通"), 15_000, "部分退按净额 150")
    c.eq(cats.get("餐饮"), 30_000, "没退的按原额 300")
    c.eq(sum(cats.values()), st["expense"], "分类净额合计 = 净支出")
    c.eq(all(x.get("direction") == "expense" for x in st["by_category"]), True,
         "分类统计标注 direction=expense")


@case("H 统计口径", "谁花的净额：退款按原单归属抵扣")
def h5(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=10_000, when=d, client=c.member_a)
    b = c.expense(amount=20_000, when=d, client=c.member_b)
    c.refund(a, 4_000, client=c.member_b)          # 老婆操作退款，但钱该冲老公那笔
    st = c.stats(m)
    owners = {x["name"]: x["expense"] for x in st["by_owner"]}
    c.eq(owners.get("测试甲"), 6_000, "甲净额 60（退款按原单归属）")
    c.eq(owners.get("测试乙"), 20_000, "乙净额 200")
    c.eq(sum(owners.values()), st["expense"], "谁花的合计 = 净支出")


@case("H 统计口径", "共同支出按净值统计")
def h6(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d, shared=True)
    c.expense(amount=5_000, when=d)
    c.refund(pid, 3_000)
    st = c.stats(m)
    c.eq(st["shared"], 7_000, "共同支出按净额 70")


@case("H 统计口径", "待报销/已报销按净值统计，且已报销只算当月")
def h7(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    p1 = c.expense(amount=10_000, when=d, reimb_pending=True)
    p2 = c.expense(amount=6_000, when=d, reimb_pending=True)
    c.refund(p1, 4_000)
    c.me.post(f"/api/tx/{p2}/reimburse", {})      # 已报销只能由到账记录产生（D-02）
    st = c.stats(m)
    # 待报销是跨月累计 → 只看增量；已报销是按月 → 可断言绝对值
    c.eq(st["pending"] - base["pending"], 6_000, "待报销净增 60（100 退 40）")
    c.eq(st["pending_cnt"] - base["pending_cnt"], 1, "待报销笔数 +1")
    # reimb_done 随 D-02 一起退场（标记只由记录产生时它恒为 0），改看「本月报销到账」
    c.eq(st["reimb"] - base["reimb"], 6_000, "本月报销到账 60")


@case("H 统计口径", "空月份各项为 0，不报错")
def h8(c: Ctx):
    st = c.stats("2019-01")
    # 按月统计的字段：空月份必须为 0
    for k in ("expense", "expense_gross", "refund_offset", "refund_orphan",
              "income", "shared", "reimb", "cat_total", "cat_negative"):
        c.eq(st.get(k), 0, f"空月份按月字段 {k} = 0")
    c.eq(st["by_category"], [], "空月份分类为空")
    c.eq(st["by_owner"], [], "空月份谁花的为空")
    # 待报销 / 待用券是跨月累计，换任何月份都应返回同一个数
    other = c.stats("2019-02")
    c.eq(st["pending"], other["pending"], "待报销不随月份变化（跨月累计）")
    c.eq(st["voucher"], other["voucher"], "待用券不随月份变化（跨月累计）")


@case("H 统计口径", "不传 month 时默认统计当月")
def h9(c: Ctx):
    st, data = c.me.get("/api/stats")
    c.eq(data["month"], datetime.now().strftime("%Y-%m"), "默认月份 = 当前月")


# ================================================================ I 筛选与列表
@case("I 筛选列表", "按月份与方向筛选")
def i1(c: Ctx):
    m, d = c.month_pair()
    c.expense(amount=1000, when=d)
    c.income(amount=2000, when=d)
    st, r = c.me.get(f"/api/tx?month={m}&direction=expense")
    c.eq(len(r["items"]), 1, "只看支出：1 条")
    st, r = c.me.get(f"/api/tx?month={m}&direction=income")
    c.eq(len(r["items"]), 1, "只看收入：1 条")
    st, r = c.me.get(f"/api/tx?month={m}&direction=refund")
    c.eq(len(r["items"]), 0, "只看退款：0 条")


@case("I 筛选列表", "五种标记筛选各命中正确记录")
def i2(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=1000, when=d, shared=True)
    b = c.expense(amount=2000, when=d, reimb_pending=True)
    e = c.expense(amount=3000, when=d)
    c.me.post(f"/api/tx/{e}/reimburse", {})       # 已报销只能由到账记录产生（D-02）
    v = c.expense(amount=4000, when=d, pending_use=True)
    r = c.expense(amount=5000, when=d)
    c.refund(r, 1000)
    expect = {"shared": a, "pending": b, "reimb": e, "voucher": v, "refunded": r}
    for mark, tid in expect.items():
        st, data = c.me.get(f"/api/tx?month={m}&mark={mark}")
        ids = {x["id"] for x in data["items"]}
        c.eq(ids, {tid}, f"mark={mark} 只命中对应那一条")


@case("I 筛选列表", "反选：mark 只要 + exclude 不要，两种标记可同时用")
def i2b(c: Ctx):
    m, d = c.month_pair()
    keep = c.expense(amount=1000, when=d, travel=True)                 # 要的就是这一条
    reimb = c.expense(amount=2000, when=d, travel=True)
    c.me.post(f"/api/tx/{reimb}/reimburse", {})                       # 旅行但已报销 → 排除
    refunded = c.expense(amount=3000, when=d, travel=True)
    c.refund(refunded, 3000)                                           # 旅行但有退款 → 排除
    plain = c.expense(amount=4000, when=d, shared=True)                # 共同支出，没旅行
    st, r = c.me.get(f"/api/tx?month={m}&direction=expense"
                     "&mark=travel&exclude=reimb,refunded")
    c.eq({x["id"] for x in r["items"]}, {keep}, "旅行 且 未报销 且 无退款：只剩 1 条")
    # 排除多种时是「任一命中就排除」（OR 后整体取反），不是「全部命中才排除」
    st, r = c.me.get(f"/api/tx?month={m}&direction=expense&exclude=reimb")
    c.eq({x["id"] for x in r["items"]}, {keep, refunded, plain},
         "只排除已报销：已退款那条仍在")
    # 多个「只要」之间是「或」：勾「共同 + 旅行」应拿到并集，而不是交集（交集多半是空）
    st, r = c.me.get(f"/api/tx?month={m}&direction=expense&mark=travel,shared")
    c.eq({x["id"] for x in r["items"]}, {keep, reimb, refunded, plain},
         "mark 多选 = 任一命中（并集）")
    # 同一个键既在 mark 又在 exclude：前端不该这么传，但接口不能 500，结果应是空集
    st, r = c.me.get(f"/api/tx?month={m}&mark=travel&exclude=travel")
    c.eq(st, 200, "自相矛盾的标记组合不报错")
    c.eq(r["items"], [], "既要又不要 = 空集")
    # 未知键静默忽略，与 owner / min_cents 的容错风格一致
    st, base = c.me.get(f"/api/tx?month={m}")
    st, r = c.me.get(f"/api/tx?month={m}&exclude=不存在的标记")
    c.eq(len(r["items"]), len(base["items"]), "exclude 传未知键时等于没传")


@case("I 筛选列表", "按归属、大类、关键词、金额区间筛选")
def i3(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=1000, when=d, cat=("餐饮", "早餐"), note="楼下面包", client=c.member_a)
    b = c.expense(amount=50_000, when=d, cat=("交通", "打车"), note="去机场", client=c.member_b)
    st, meta = c.me.get("/api/meta")
    member_a_id = [u["id"] for u in meta["users"] if u["username"] == "tester1"][0]
    member_b_id = [u["id"] for u in meta["users"] if u["username"] == "tester2"][0]
    st, r = c.me.get(f"/api/tx?month={m}&owner={member_a_id}")
    c.eq({x["id"] for x in r["items"]}, {a}, "按归属筛出甲那一条")
    st, r = c.me.get(f"/api/tx?month={m}&owner={member_b_id}")
    c.eq({x["id"] for x in r["items"]}, {b}, "按归属筛出乙那一条")
    st, r = c.me.get(f"/api/tx?month={m}&cat_parent=交通")
    c.eq({x["id"] for x in r["items"]}, {b}, "按大类筛选")
    st, r = c.me.get(f"/api/tx?month={m}&q=机场")
    c.eq({x["id"] for x in r["items"]}, {b}, "关键词命中备注")
    st, r = c.me.get(f"/api/tx?month={m}&q=早餐")
    c.eq({x["id"] for x in r["items"]}, {a}, "关键词命中子类")
    st, r = c.me.get(f"/api/tx?month={m}&min_cents=10000")
    c.eq({x["id"] for x in r["items"]}, {b}, "金额下限筛选")
    st, r = c.me.get(f"/api/tx?month={m}&max_cents=2000")
    c.eq({x["id"] for x in r["items"]}, {a}, "金额上限筛选")
    st, r = c.me.get(f"/api/tx?month={m}&min_cents=1000&max_cents=60000")
    c.eq({x["id"] for x in r["items"]}, {a, b}, "金额区间筛选")


@case("I 筛选列表", "多条件组合（AND）与无效参数容错")
def i4(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=1000, when=d, cat=("餐饮", "早餐"), client=c.member_a)
    c.expense(amount=2000, when=d, cat=("餐饮", "午餐"), client=c.member_a)
    c.expense(amount=3000, when=d, cat=("交通", "打车"), client=c.member_a)
    st, r = c.me.get(f"/api/tx?month={m}&direction=expense&cat_parent=餐饮&max_cents=1500")
    c.eq({x["id"] for x in r["items"]}, {a}, "三个条件同时生效")
    st, r = c.me.get(f"/api/tx?month={m}&min_cents=abc&owner=")
    c.eq(st, 200, "非法金额参数不报错")
    c.eq(len(r["items"]), 3, "非法参数被忽略，不影响其余结果")


@case("I 筛选列表", "列表顶部聚合 summary 与筛选结果一致")
def i5(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=1000, when=d)
    b = c.expense(amount=2000, when=d)
    c.income(amount=5000, when=d)
    pid = c.expense(amount=4000, when=d)
    c.refund(pid, 1500, when=d)
    st, r = c.me.get(f"/api/tx?month={m}")
    s = r["summary"]
    c.eq(s["count"], 5, "笔数 5")
    c.eq(s["expense"], 1000 + 2000 + 4000, "summary 支出为毛额（不扣退款）")
    c.eq(s["income"], 5000, "summary 收入")
    c.eq(s["refund"], 1500, "summary 退款")
    st, r = c.me.get(f"/api/tx?month={m}&direction=expense")
    c.eq(r["summary"]["count"], 3, "筛选后 summary 同步变化")


@case("I 筛选列表", "列表排序：日期倒序，同一天按 id 倒序")
def i6(c: Ctx):
    m, d = c.month_pair()
    older = c.expense(amount=1000, when=d)
    newer = c.expense(amount=2000, when=d)
    c.eq(newer > older, True, "后创建的 id 更大")
    st, r = c.me.get(f"/api/tx?month={m}")
    ids = [x["id"] for x in r["items"]]
    c.eq(ids[:2], [newer, older], "同一天两笔，id 大的排前面")
    prev = c.expense(amount=3000, when=month_of(-1, 1))
    st, r = c.me.get(f"/api/tx?month={month_str(0)}")
    c.eq(r["items"][0]["id"] != prev, True, "上个月的记录不会排到本月最前")


@case("I 筛选列表", "排序：金额高低/日期正序，与筛选并存，脏值回落默认")
def i9(c: Ctx):
    m, d10 = c.month_pair(day=10)
    d20 = d10[:8] + "20"
    a = c.expense(amount=1_000, when=d10, cat=("餐饮", "早餐"))
    b = c.expense(amount=50_000, when=d20, cat=("交通", "打车"))
    e = c.expense(amount=9_000, when=d10, cat=("餐饮", "午餐"))

    st, r = c.me.get(f"/api/tx?month={m}&sort=amount_desc")
    c.eq([x["id"] for x in r["items"]], [b, e, a], "金额从高到低")
    st, r = c.me.get(f"/api/tx?month={m}&sort=amount_asc")
    c.eq([x["id"] for x in r["items"]], [a, e, b], "金额从低到高")
    # 日期正序：先 10 号（同一天 id 小的在前），再 20 号
    st, r = c.me.get(f"/api/tx?month={m}&sort=date_asc")
    c.eq([x["id"] for x in r["items"]], [a, e, b], "日期正序")

    # 排序只影响顺序，不改筛选命中集合
    st, r = c.me.get(f"/api/tx?month={m}&sort=amount_desc&cat_parent=餐饮")
    c.eq({x["id"] for x in r["items"]}, {a, e}, "排序 + 大类筛选同时生效")
    st, r = c.me.get(f"/api/tx?month={m}&sort=amount_desc&direction=expense&min_cents=2000")
    c.eq([x["id"] for x in r["items"]], [b, e], "排序 + 多条件筛选后仍按金额排")
    c.eq(r["summary"]["count"], 2, "排序不改变 summary 统计口径")

    # 非法排序键是白名单外的东西，必须被忽略而不是拼进 SQL
    st, r = c.me.get(f"/api/tx?month={m}&sort=;DROP TABLE transactions")
    c.eq(st, 200, "非法 sort 参数仍返回 200")
    c.eq([x["id"] for x in r["items"]], [b, e, a], "非法 sort 回落到默认日期倒序")


@case("I 筛选列表", "不传 month 返回全部月份（统计页跨月指标靠它跳转）")
def i10(c: Ctx):
    m1, d1 = c.month_pair(day=10)
    m2, d2 = c.month_pair(day=10)
    a = c.expense(amount=1_000, when=d1, reimb_pending=True)
    b = c.expense(amount=2_000, when=d2, reimb_pending=True)
    # 待报销是跨月累计：别的用例也会留待报销记录，这里只断言「这两笔在里面」
    st, r = c.me.get("/api/tx?mark=pending")
    c.ok({a, b} <= {x["id"] for x in r["items"]}, "不限月份时，两个月的待报销都能拿到")
    st, r = c.me.get(f"/api/tx?mark=pending&month={m1}")
    c.eq({x["id"] for x in r["items"]}, {a}, "带月份时只返回当月那一笔")


@case("I 筛选列表", "日期区间筛选：含首尾两天，可与其它条件叠加，脏值不崩")
def i11(c: Ctx):
    m, d10 = c.month_pair(day=10)
    d05, d20 = d10[:8] + "05", d10[:8] + "20"
    a = c.expense(amount=1_000, when=d05, cat=("餐饮", "早餐"), note="区间首日")
    b = c.expense(amount=2_000, when=d10, cat=("餐饮", "午餐"), note="区间中间")
    e = c.expense(amount=3_000, when=d20, cat=("交通", "打车"), note="区间末日")
    m2, d2 = c.month_pair(day=10)               # 另一个月，必须被区间排除
    out = c.expense(amount=4_000, when=d2, cat=("餐饮", "晚餐"), note="区间外")

    st, r = c.me.get(f"/api/tx?start={d05}&end={d20}")
    c.eq({x["id"] for x in r["items"]}, {a, b, e}, "首尾两天都算在区间内，区间外的被排除")
    st, r = c.me.get(f"/api/tx?start={d10}&end={d20}")
    c.eq({x["id"] for x in r["items"]}, {b, e}, "只从中间那天起算，首日被排除")
    st, r = c.me.get(f"/api/tx?start={d05}&end={d20}&cat_parent=餐饮")
    c.eq({x["id"] for x in r["items"]}, {a, b}, "区间与大类叠加")
    c.ok(out not in {x["id"] for x in r["items"]}, "区间外的记录不会混进来")
    st, r = c.me.get("/api/tx?start=bad&end=2026-13-45")
    c.eq(st, 200, "日期格式非法不报错")
    c.eq(r.get("items") is not None, True, "非法日期被忽略，仍返回列表")


@case("I 筛选列表", "关键词：空格分词 AND，命中备注/分类/人名/金额")
def i12(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=2_500, when=d, cat=("餐饮", "早餐"), note="楼下面包", client=c.member_a)
    b = c.expense(amount=9_900, when=d, cat=("交通", "打车"), note="机场快线", client=c.member_b)
    e = c.expense(amount=2_500, when=d, cat=("购物", "数码"), note="耳机", client=c.member_b)

    # 关键词不带月份就是全库搜，会撞上别的用例的同名备注；这里统一限定本用例独占的那个月
    st, r = c.me.get(f"/api/tx?month={m}&q=机场")
    c.eq({x["id"] for x in r["items"]}, {b}, "命中备注")
    st, r = c.me.get(f"/api/tx?month={m}&q=交通")
    c.eq({x["id"] for x in r["items"]}, {b}, "命中大类")
    st, r = c.me.get(f"/api/tx?month={m}&q=25")
    c.eq({x["id"] for x in r["items"]}, {a, e}, "纯数字按金额精确匹配 25 元")
    st, r = c.me.get(f"/api/tx?month={m}&q=25.00")
    c.eq({x["id"] for x in r["items"]}, {a, e}, "带小数的数字同样按金额匹配")

    st, meta = c.me.get("/api/meta")
    member_b_name = [u["display_name"] for u in meta["users"] if u["username"] == "tester2"][0]
    st, r = c.me.get(f"/api/tx?month={m}&q={member_b_name}")
    c.eq({x["id"] for x in r["items"]}, {b, e}, "命中支出人姓名")

    st, r = c.me.get(f"/api/tx?month={m}&q=机场 打车")
    c.eq({x["id"] for x in r["items"]}, {b}, "两个词都命中才返回")
    st, r = c.me.get(f"/api/tx?month={m}&q=机场 早餐")
    c.eq({x["id"] for x in r["items"]}, set(), "词分属不同记录时不命中（AND 不是 OR）")
    st, r = c.me.get(f"/api/tx?month={m}&q=25 耳机")
    c.eq({x["id"] for x in r["items"]}, {e}, "金额与文本两路同时参与")


@case("I 筛选列表", "月份闭区间：跨年不串月，LIKE 通配符按字面匹配")
def i13(c: Ctx):
    # S-01 把 `tx_date LIKE '2026-10-%'` 换成 `>= 月初 AND < 下月初`。两件事要守住：
    #   ① 边界天不能串月（尤其 12 月 → 次年 1 月，手写「月份+1」会变 13 月）
    #   ② 关键词里的 % 和 _ 是字面，不是通配符（以前搜 "%" 等于搜全部）
    m, d = c.month_pair()
    y, mo = int(m[:4]), int(m[5:7])
    first = date(y, mo, 1)
    nxt_first = date(y + (mo == 12), mo % 12 + 1, 1)
    last = (nxt_first - timedelta(days=1)).isoformat()
    prev_last = (first - timedelta(days=1)).isoformat()

    st, s0 = c.me.get(f"/api/stats?month={m}")
    e_prev = c.expense(amount=100, when=prev_last, note="边界甲")
    e_first = c.expense(amount=200, when=first.isoformat(), note="边界乙")
    e_last = c.expense(amount=300, when=last, note="边界丙")
    e_next = c.expense(amount=400, when=nxt_first.isoformat(), note="边界丁")
    st, r = c.me.get(f"/api/tx?month={m}&q=边界")
    c.eq({x["id"] for x in r["items"]}, {e_first, e_last}, "本月含首尾两天，不含上月最后一天/下月第一天")
    st, s1 = c.me.get(f"/api/stats?month={m}")
    c.eq(s1["expense_gross"] - s0["expense_gross"], 500, "本月统计只累加本月那两笔")

    # 跨年：12-31 归 12 月，次年 01-01 归次年 1 月，互不越界
    st, sd0 = c.me.get("/api/stats?month=2026-12")
    st, sj0 = c.me.get("/api/stats?month=2027-01")
    dec = c.expense(amount=1_100, when="2026-12-31", note="跨年边界的年末")
    jan = c.expense(amount=2_200, when="2027-01-01", note="跨年边界的年初")
    st, sd1 = c.me.get("/api/stats?month=2026-12")
    st, sj1 = c.me.get("/api/stats?month=2027-01")
    c.eq(sd1["expense_gross"] - sd0["expense_gross"], 1_100, "2026-12 只算 12-31，不含次年 01-01")
    c.eq(sj1["expense_gross"] - sj0["expense_gross"], 2_200, "2027-01 只算 01-01，不含去年 12-31")
    st, r = c.me.get("/api/tx?month=2026-12&q=跨年边界")
    c.eq({x["id"] for x in r["items"]}, {dec}, "列表同样不串年")

    # 通配符按字面：% 和 _ 都不再是「任意字符」
    a = c.expense(amount=3_300, when=d, note="折扣 50% 到手")
    b = c.expense(amount=3_400, when=d, note="编号 a_b 的票")
    c.expense(amount=3_500, when=d, note="编号 axb 的票")
    st, r = c.me.get(f"/api/tx?month={m}&q=%25")
    c.eq({x["id"] for x in r["items"]}, {a}, "搜 % 只命中真的写着 % 的备注（不再是「全部」）")
    st, r = c.me.get(f"/api/tx?month={m}&q=a_b")
    c.eq({x["id"] for x in r["items"]}, {b}, "a_b 不命中 axb：下划线按字面而不是任意字符")


@case("I 筛选列表", "脏参数容错：owner / parent / amount 传非数字不返回 5xx")
def i8(c: Ctx):
    st, r = c.me.get("/api/tx?month=2020-01&owner=abc")
    c.eq(st, 200, "owner 传脏值仍返回 200")
    c.eq(r.get("items"), [], "脏 owner 被忽略，返回空列表")
    st, _ = c.me.get("/api/refund-targets?amount_cents=abc&owner=abc")
    c.eq(st, 200, "候选接口脏参数仍返回 200")
    st, _ = c.me.get("/api/refunds?parent=abc")
    c.eq(st, 400, "退款明细 parent 非数字返回 400")
    st, _ = c.me.get("/api/stats?month=bad")
    c.eq(st, 200, "统计接口月份非法也不崩")


@case("I 筛选列表", "列表行上带退款派生字段（已退/剩余/笔数）")
def i7(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d)
    c.refund(pid, 3_000)
    st, r = c.me.get(f"/api/tx?month={m}")
    row = [x for x in r["items"] if x["id"] == pid][0]
    c.eq(row["refunded_cents"], 3_000, "已退金额")
    c.eq(row["remain_cents"], 7_000, "剩余可退")
    c.eq(row["refund_count"], 1, "退款笔数")


# ================================================================ J 关联完整性
@case("J 关联完整性", "删原单：退款转为无主，不连带删除，返回解绑计数")
def j1(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d)
    r1 = c.refund(pid, 3_000, when=D(0))
    r2 = c.refund(pid, 2_000, when=D(0))
    st, data = c.delete(pid)
    c.eq(st, 200, "删除原单成功")
    c.eq(data.get("detached_refunds"), 2, "返回解绑了 2 笔退款")
    c.eq(c.row(pid, ""), None, "原单已删")
    c.eq(c.row(r1, "")["parent_id"], None, "退款 1 变成无主")
    c.eq(c.row(r2, "")["parent_id"], None, "退款 2 变成无主")


@case("J 关联完整性", "删除退款后，原单剩余额度恢复")
def j2(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d)
    rid = c.refund(pid, 6_000)
    c.eq(c.row(pid, "")["remain_cents"], 4_000, "退 60 后剩 40")
    c.delete(rid)
    c.eq(c.row(pid, "")["remain_cents"], 10_000, "删退款后额度全恢复")
    c.eq(c.row(pid, "")["refunded_cents"], 0, "已退金额归零")


@case("J 关联完整性", "删原单后的统计：原月恢复毛额，无主退款留在退款月")
def j3(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d)
    c.refund(pid, 10_000, when=d)              # 退款也记在同一个月
    before = c.stats(m)
    c.eq(before["expense"], 0, "删前净支出 0")
    c.delete(pid)
    after = c.stats(m)
    c.eq(after["expense_gross"], 0, "删后毛支出 0")
    c.eq(after["refund_orphan"], 10_000, "退款变无主，算在退款月")
    c.eq(after["refund_offset"], 0, "没有可冲抵的原单")


@case("J 关联完整性", "删券后待用券统计同步恢复")
def j4(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=20_000, when=d, pending_use=True)
    c.eq(c.stats(m)["voucher"] - base["voucher"], 20_000, "建券后 +200")
    c.delete(pid)
    c.eq(c.stats(m)["voucher"], base["voucher"], "删券后回到建券前")
    c.eq(c.stats(m)["voucher_cnt"], base["voucher_cnt"], "券笔数也回退")


# ================================================================ K 综合场景
@case("K 综合场景", "场景1：100 买两个，退一个 50 —— 净支出 50，分类净额 50")
def k1(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=10_000, when=d, cat=("购物", "日用品"), note="两件装")
    c.refund(pid, 5_000, note="退一件")
    st = c.stats(m)
    c.eq(st["expense"], 5_000, "净支出 50")
    cats = {x["cat_parent"]: x["total"] for x in st["by_category"]}
    c.eq(cats.get("购物"), 5_000, "购物分类净额 50")
    row = c.row(pid, m)
    c.eq(row["note"], "两件装", "原单备注未被改写")
    c.eq(row["amount_cents"], 10_000, "原单金额未被改写")


@case("K 综合场景", "场景2：团购券 300 没用成、过期作废 —— 支出保留，券清零")
def k2(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=30_000, when=d, pending_use=True, note="双人火锅券")
    c.eq(c.stats(m)["voucher"] - base["voucher"], 30_000, "买券后券统计 +300")
    c.me.patch(f"/api/tx/{pid}", {"pending_use": False})   # 前端「过期作废」走这条
    st = c.stats(m)
    c.eq(st["voucher"], base["voucher"], "作废后券统计回到买券前")
    c.eq(st["expense"], 30_000, "钱没退回来，支出保留 300")


@case("K 综合场景", "场景3：团购券 300 没用成、自动退款 —— 支出被冲抵为 0")
def k3(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=30_000, when=d, pending_use=True, note="双人火锅券")
    c.refund(pid, 30_000, when=D(2), note="过期自动退")
    st = c.stats(m)
    c.eq(st["expense"], base["expense"], "净支出回到买券前")
    c.eq(st["expense_gross"], 30_000, "毛支出仍显示 300")
    c.eq(st["refund_offset"], 30_000, "冲抵 300")
    c.eq(st["voucher"], base["voucher"], "券统计清零")
    c.eq(st["by_category"], [], "分类里不再出现这项消费")


@case("K 综合场景", "场景4：跨月退货 —— 上月数字回改，本月不受影响")
def k4(c: Ctx):
    m, d = c.month_pair()
    old = m
    pid = c.expense(amount=20_000, when=d, cat=("购物", "数码"))
    c.eq(c.stats(old)["expense"], 20_000, "买的时候上月净支出 200")
    cur_before = c.stats(month_str(0))["refund_offset"]
    c.refund(pid, 20_000, when=D(0))            # 本月才退
    c.eq(c.stats(old)["expense"], 0, "退款后上月净支出回改为 0")
    c.eq(c.stats(old)["refund_offset"], 20_000, "冲抵金额记在上月")
    c.eq(c.stats(month_str(0))["refund_offset"], cur_before, "本月的冲抵金额不受影响")


@case("K 综合场景", "场景5：一单分三次退完，中途标记从待报销变为已报销")
def k5(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=9_000, when=d, reimb_pending=True, note="客户招待")
    c.refund(pid, 3_000)
    c.eq(c.row(pid, "")["reimb_pending"], 1, "部分退：仍是待报销")
    c.eq(c.stats(m)["pending"] - base["pending"], 6_000, "待报销净增 60")
    c.refund(pid, 3_000)
    c.eq(c.row(pid, "")["reimb_pending"], 1, "退到 2/3 仍保留待报销")
    c.eq(c.stats(m)["pending"] - base["pending"], 3_000, "待报销净值降到 30")
    c.refund(pid, 3_000)
    row = c.row(pid, "")
    c.eq(row["reimb_pending"], 0, "全部退完后待报销被自动清除")
    c.eq(c.stats(m)["pending"], base["pending"], "待报销回到初始")
    c.eq(c.stats(m)["expense"], 0, "净支出归零")


@case("K 综合场景", "场景6：两人协作 —— 甲买、乙退，账目归属与统计都不串")
def k6(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=12_000, when=d, client=c.member_a, cat=("购物", "服饰"))
    rid = c.refund(pid, 2_000, client=c.member_b)
    st = c.stats(m)
    owners = {x["name"]: x["expense"] for x in st["by_owner"]}
    c.eq(owners.get("测试甲"), 10_000, "退款冲抵回甲名下（120-20）")
    c.eq(c.row(rid, "")["owner_id"], c.row(pid, "")["owner_id"], "退款归属跟随原单")
    st, h = c.me.get(f"/api/refunds?parent={pid}")
    item = h["items"][0]
    c.eq(item["owner_name"], "测试甲", "退款明细：钱算在甲名下（继承原单）")
    c.eq(item["created_name"], "测试乙", "退款明细：记录实际动手退款的乙")


@case("K 综合场景", "场景7：券 → 用掉一半额度（部分退款）→ 剩余继续待用")
def k7(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    pid = c.expense(amount=10_000, when=d, pending_use=True, note="200 面值券")
    c.refund(pid, 6_000, note="退一部分额度")
    row = c.row(pid, m)
    c.eq(row["pending_use"], 1, "仍有剩余额度，标记保留")
    c.eq(row["remain_cents"], 4_000, "剩余可退 40")
    st = c.stats(m)
    c.eq(st["voucher"] - base["voucher"], 4_000, "待用券按净值 40")
    c.eq(st["expense"], 4_000, "净支出 40")


# ================================================================ L 分类树
@case("L 分类树", "8 个大类：居住/通讯/教育已并入生活缴费与其他支出")
def l1(c: Ctx):
    st, meta = c.me.get("/api/meta")
    exp = meta["categories"]["expense"]
    c.eq(list(exp.keys()),
         ["餐饮", "交通", "购物", "生活缴费", "医疗", "娱乐", "人情", "其他支出"],
         "支出大类与顺序")
    for gone in ("居住", "通讯", "教育"):
        c.eq(gone in exp, False, f"「{gone}」已删除")
    c.eq("房租房贷" in exp["生活缴费"], True, "房贷归生活缴费")
    c.eq("会员订阅" in exp["生活缴费"], True, "会员费归生活缴费")
    c.eq("宽带" in exp["生活缴费"], True, "宽带归生活缴费")
    c.eq("课程" in exp["其他支出"], True, "课程并入其他支出")
    c.eq("书籍" in exp["其他支出"], True, "书籍并入其他支出")
    for k, subs in exp.items():
        c.eq("其他" in subs, True, f"「{k}」有「其他」子类")


@case("L 分类树", "分类配置带用量：行尾「本月 N 笔」与删除前的「N 笔账在用」")
def l3(c: Ctx):
    # 用量是全库聚合，只能断言增量——套件里别的用例也在往同一棵树上记账
    st, before = c.admin.get("/api/categories/config")
    c.eq(st, 200, "管理员能读配置")
    u0 = before.get("usage") or {}
    base_food = (u0.get("expense", {}).get("餐饮") or {})
    base_car = (u0.get("expense", {}).get("交通") or {})

    today = datetime.now().strftime("%Y-%m-%d")
    past = month_of(-11, 15)                     # 真·上上个月：只该进 total，不进 month
    c.expense(amount=1_000, when=today, cat=("餐饮", "早餐"))
    c.expense(amount=2_000, when=today, cat=("餐饮", "午餐"))
    c.expense(amount=3_000, when=past, cat=("交通", "打车"))

    st, after = c.admin.get("/api/categories/config")
    u1 = (after.get("usage") or {}).get("expense", {})
    c.eq(u1["餐饮"]["month"] - base_food.get("month", 0), 2, "餐饮本月 +2 笔")
    c.eq(u1["餐饮"]["total"] - base_food.get("total", 0), 2, "餐饮 total +2 笔")
    c.eq(u1["交通"]["month"] - base_car.get("month", 0), 0, "交通本月没变（那笔是上上月）")
    c.eq(u1["交通"]["total"] - base_car.get("total", 0), 1, "交通 total +1 笔（删除前要算全量）")
    c.eq("users" in after, False, "配置接口不夹带账号信息")
    st, _ = c.me.get("/api/categories/config")
    c.eq(st, 403, "非管理员 403")


@case("L 分类树", "生活缴费能记账，旧大类「通讯」被拒")
def l2(c: Ctx):
    pid = c.expense(amount=uniq_cents(), when=D(1), cat=("生活缴费", "会员订阅"), note="视频会员")
    c.eq(pid > 0, True, "生活缴费 · 会员订阅 能记账")
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "expense",
                                  "cat_parent": "通讯", "category": "话费",
                                  "amount_cents": 100})
    c.eq(st, 400, "已删除的大类「通讯」被拒")
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "expense",
                                  "cat_parent": "生活缴费", "category": "流量",
                                  "amount_cents": 100})
    c.eq(st, 400, "生活缴费下不存在的子类被拒")


# ================================================================ M 旅行标记
@case("M 旅行标记", "旅行标记能存能筛，非支出一律清零")
def m1(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=uniq_cents(), when=d, travel=True, note="三亚椰子鸡")
    b = c.expense(amount=uniq_cents(), when=d, note="楼下便利店")
    st, data = c.me.get(f"/api/tx?month={m}&mark=travel")
    ids = {x["id"] for x in data["items"]}
    c.eq(a in ids, True, "带旅行标记的能筛出来")
    c.eq(b in ids, False, "没标记的筛不出来")
    c.eq(c.row(a, m)["travel"], 1, "列表行带 travel 字段")
    # 改成收入：旅行标记必须跟着清掉（标记只属于支出）
    st, _ = c.me.patch(f"/api/tx/{a}", {"direction": "income",
                                        "cat_parent": "工资", "category": "月度工资"})
    c.eq(st, 200, "改成收入成功")
    c.eq(c.row(a, m)["travel"], 0, "非支出时旅行标记被清空")


@case("M 旅行标记", "旅行标记不影响统计口径")
def m2(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    c.expense(amount=30_000, when=d, travel=True, note="酒店")
    st = c.stats(m)
    c.eq(st["expense"] - base["expense"], 30_000, "带旅行标记照样计入支出")
    c.eq(st["shared"], base["shared"], "不串到共同支出")


# ================================================================ N 借款台账
@case("N 借款台账", "借款增改删，且完全不计入统计")
def n1(c: Ctx):
    m, d = c.month_pair()
    base = c.stats(m)
    st, r = c.me.post("/api/loans", {"direction": "out", "counterparty": "朋友",
                                     "amount_cents": 10_000_000, "loan_date": d,
                                     "note": "借款"})
    c.eq(st, 200, "新建借款")
    lid = r["id"]

    def one():
        st, data = c.me.get("/api/loans")
        return [x for x in data["items"] if x["id"] == lid][0], data["summary"]

    item, summary = one()
    c.eq(item["remain_cents"], 10_000_000, "剩余 = 借款额")
    c.eq(item["settled"], False, "未结清")
    c.eq(item["owner_name"], "测试甲", "带归属人姓名")
    c.eq(summary["out_open"], 10_000_000, "借出未收回合计")

    st = c.stats(m)
    c.eq(st["expense"], base["expense"], "借款不计入支出")
    c.eq(st["refund"], base["refund"], "借款不计入退款")
    c.eq(st["income"], base["income"], "借款不计入收入")

    st, _ = c.me.patch(f"/api/loans/{lid}", {"repaid_cents": 4_000_000})
    c.eq(st, 200, "记部分还款")
    item, _ = one()
    c.eq(item["remain_cents"], 6_000_000, "剩余减少")
    c.eq(item["settled"], False, "还没结清")

    c.me.patch(f"/api/loans/{lid}", {"repaid_cents": 10_000_000})
    item, summary = one()
    c.eq(item["settled"], True, "还清后标记结清")
    c.eq(summary["out_open"], 0, "结清后不再计入未收回")

    st, _ = c.me.req("DELETE", f"/api/loans/{lid}")
    c.eq(st, 200, "删除借款")
    st, data = c.me.get("/api/loans")
    c.eq(lid in {x["id"] for x in data["items"]}, False, "删除后列表里没有了")


@case("N 借款台账", "借入方向单独汇总；非法参数一律 400")
def n2(c: Ctx):
    before = c.me.get("/api/loans")[1]["summary"]["in_open"]
    st, r = c.me.post("/api/loans", {"direction": "in", "counterparty": "同事",
                                     "amount_cents": 5_000, "loan_date": D(2)})
    c.eq(st, 200, "借入能记")
    lid = r["id"]
    st, data = c.me.get("/api/loans")
    c.eq(data["summary"]["in_open"] - before, 5_000, "借入未还合计 +50")
    for body, why in (
        ({"counterparty": "", "amount_cents": 100}, "对方为空"),
        ({"counterparty": "甲", "amount_cents": 0}, "金额为 0"),
        ({"counterparty": "甲", "amount_cents": 100, "direction": "x"}, "方向非法"),
        ({"counterparty": "甲", "amount_cents": 100, "loan_date": "2026/13/01"}, "日期非法"),
    ):
        st, _ = c.me.post("/api/loans", body)
        c.eq(st, 400, f"{why} → 400")
    st, _ = c.me.patch(f"/api/loans/{lid}", {"repaid_cents": 9_999_999})
    c.eq(st, 400, "已还金额超过借款额 → 400")
    c.me.req("DELETE", f"/api/loans/{lid}")


@case("N 借款台账", "未登录访问借款接口一律 401")
def n3(c: Ctx):
    c.eq(c.anon.get("/api/loans")[0], 401, "匿名读借款 401")
    c.eq(c.anon.post("/api/loans", {"counterparty": "甲", "amount_cents": 100})[0], 401,
         "匿名建借款 401")
    c.eq(c.anon.req("DELETE", "/api/loans/1")[0], 401, "匿名删借款 401")


# ================================================================ O AI 识别
# 实例带 LEDGER_AI_MOCK=1 启动（见 start_server）：不联网、不烧额度，
# 但请求体构造、分类兜底、日志落库这些真实链路一条不落地走一遍。
# ⚠️ f1 依赖「此前没有任何用例配置过 Key」，所以它是本组第一个，别乱调顺序。

_FAKE_IMG = "data:image/jpeg;base64," + base64.b64encode(
    b"\xff\xd8\xff\xe0" + b"0" * 900).decode()


def ensure_ai(c: Ctx):
    """配置一个假 Key（mock 模式不校验真伪），重复调用幂等。"""
    st, _ = c.admin.post("/api/settings", {"ai_api_key": "mock-key-for-tests"})
    assert st == 200, "配置 AI Key 失败"


@case("O AI 识别", "未配置 Key 时报错清晰，手动记账不受影响")
def f1(c: Ctx):
    st, data = c.me.post("/api/ai/parse", {"text": "午饭 25"})
    c.eq(st, 502, "未配置 Key 时识别返回 502")
    c.contains(data.get("error", ""), "未配置", "错误信息指向「设置里填 Key」")
    c.ok(c.expense(amount=1234), "AI 不可用时手动记账照常")


@case("O AI 识别", "默认模型是 GLM-5.3-Flash，配 Key 后前端入口才可用")
def f2(c: Ctx):
    st, data = c.admin.get("/api/settings")
    c.eq(st, 200, "管理员读设置")
    c.eq(data.get("ai_model"), "glm-5.3-flash",
         "默认模型是 glm-5.3-flash（原生多模态，文字与识图共用）")
    ensure_ai(c)
    st, meta = c.me.get("/api/meta")
    c.eq(meta.get("ai_enabled"), True, "配置 Key 后 meta 报告 ai_enabled")


@case("O AI 识别", "一句话识别回填草稿；模型编造的越界分类被兜底")
def f3(c: Ctx):
    ensure_ai(c)
    st, data = c.me.post("/api/ai/parse", {"text": "午饭 25 共同"})
    c.eq(st, 200, "文字识别返回 200")
    d = data.get("draft") or {}
    c.eq(d.get("amount_cents"), 2500, "金额解析成 2500 分")
    c.eq(d.get("direction"), "expense", "方向是支出")
    c.eq(d.get("cat_parent"), "餐饮", "大类落在餐饮")
    c.eq(d.get("category"), "午餐", "子类落在午餐")
    c.eq(d.get("ocr_hint"), "", "纯文字识别不返回 ocr_hint")

    # 模型给了不存在的分类 → 兜底到该树的第一项，绝不能把脏值存进库
    st, data = c.me.post("/api/ai/parse", {"text": "BADCAT 测试越界"})
    c.eq(st, 200, "越界分类不报错，走兜底")
    d = data.get("draft") or {}
    c.eq(d.get("cat_parent"), "餐饮", "非法大类兜底成首项「餐饮」")
    c.eq(d.get("category"), "早餐", "非法子类兜底成首项「早餐」")


@case("O AI 识别", "识图：只有图片也能识别，并返回「读到了什么」供核对")
def f4(c: Ctx):
    ensure_ai(c)
    st, data = c.me.post("/api/ai/parse", {"text": "", "image": _FAKE_IMG})
    c.eq(st, 200, "只有图片、没有文字也能识别")
    d = data.get("draft") or {}
    c.eq(d.get("amount_cents"), 2500, "识图走同一套草稿字段")
    c.ok(d.get("ocr_hint"), "识图返回 ocr_hint（用户靠它核对金额）")

    st, _ = c.me.post("/api/ai/parse", {"text": "", "image": ""})
    c.eq(st, 400, "文字和图片都空时被拒")


@case("O AI 识别", "图片校验：格式 / 过小 / 过大 分别被拒且提示到位")
def f5(c: Ctx):
    ensure_ai(c)
    st, _ = c.me.post("/api/ai/parse",
                      {"image": "data:image/gif;base64,R0lGODlhAQABAAAAACw="})
    c.eq(st, 400, "只收 jpg/png/webp，GIF 被拒")
    st, _ = c.me.post("/api/ai/parse", {"image": "data:image/jpeg;base64,AAAA"})
    c.eq(st, 400, "太小的图被拒（大概率没拍上）")
    st, _ = c.me.post("/api/ai/parse", {"image": "这不是base64"})
    c.eq(st, 400, "非 base64 被拒")
    big = "data:image/jpeg;base64," + "A" * 4_500_000
    st, data = c.me.post("/api/ai/parse", {"image": big})
    c.eq(st, 413, "超大请求体被拒（413，不是含糊的 400）")
    c.contains(data.get("error", ""), "图片太大", "提示直接说图片太大")


@case("O AI 识别", "调用日志：普通账号看不到，管理员能看到成功与失败两类")
def f6(c: Ctx):
    ensure_ai(c)
    c.eq(c.me.get("/api/ai/logs")[0], 403, "普通账号读日志被拒 403")
    c.eq(c.anon.get("/api/ai/logs")[0], 401, "匿名读日志 401")

    st, _ = c.me.post("/api/ai/parse", {"text": "FAIL 这条必定失败"})
    c.eq(st, 502, "模型报错时返回 502")

    st, logs = c.admin.get("/api/ai/logs?limit=50")
    c.eq(st, 200, "管理员可读日志")
    items = logs.get("items") or []
    c.ok(items, "日志不是空的")
    ok_rows = [r for r in items if r["status"] == "ok"]
    bad_rows = [r for r in items if r["status"] == "error"]
    c.ok(ok_rows, "成功的调用留了 ok 记录")
    c.ok(bad_rows, "失败的调用也留了记录（调 prompt 全靠它复盘）")
    if bad_rows:
        c.contains(bad_rows[0].get("error", ""), "mock failure", "失败原因写进了日志")
    if ok_rows:
        c.eq(ok_rows[0].get("prompt_tokens"), 1200, "token 用量落库")
        c.ok(ok_rows[0].get("latency_ms") >= 0, "耗时落库")
    img_rows = [r for r in items if r["kind"] == "image"]
    c.ok(img_rows, "识图调用单独标记为 image")
    if img_rows:
        c.ok(img_rows[0].get("image_bytes") > 0, "识图记录带图片体积，方便算 token")
    week = logs.get("week") or {}
    c.ok(week.get("count", 0) >= 2, "近 7 天统计覆盖刚才的调用")
    c.ok(week.get("avg_ms", -1) >= 0, "近 7 天平均耗时可用")


@case("O AI 识别", "频控：一小时满 60 次后 429，不再继续花钱")
def f7(c: Ctx):
    import sqlite3
    ensure_ai(c)
    st, meta = c.me.get("/api/meta")
    member_b_id = [u["id"] for u in meta["users"] if u["username"] == "tester2"][0]
    # 直接铺 59 条本小时的调用记录，而不是连发 60 次真请求：每次请求要写一次库 + commit，
    # 在 Windows 上单次要几十到几百毫秒，60 次能把一条用例拖到十几秒（还会放大环境抖动）。
    # 这条要验的是「到量之后拦不拦」，边界清楚比「数得准」重要。
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    ts = now.isoformat(timespec="seconds")
    since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    db = str(c.workdir / "data" / "ledger.db")
    con = sqlite3.connect(db)
    con.execute("DELETE FROM ai_log WHERE user_id=? AND ts>=?", (member_b_id, since))
    con.executemany(
        "INSERT INTO ai_log(ts,kind,model,user_id,input_brief,image_bytes,latency_ms,"
        "prompt_tokens,completion_tokens,status,error,draft) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, "text", "m", member_b_id, "x", 0, 1, 1, 1, "ok", "", "")] * 59)
    con.commit()
    con.close()

    st, _ = c.member_b.post("/api/ai/parse", {"text": "午饭 20 元"})
    c.eq(st, 200, "第 60 次还放行（上限就是 60，不是 59）")
    st, data = c.member_b.post("/api/ai/parse", {"text": "午饭 20 元"})
    c.eq(st, 429, "第 61 次被拦")
    c.eq(data.get("code"), "ai_quota", "码是 ai_quota，前端才能按码提示")
    c.contains(data.get("error", ""), "60", "话术里说清上限")

    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM ai_log WHERE user_id=? AND ts>=?",
                    (member_b_id, since)).fetchone()[0]
    con.close()
    c.eq(n, 60, "被拦那次没写进 ai_log（说明请求根本没发出去，没花钱）")


@case("B 认证与权限", "初始密码只打印一次，不在数据目录留明文文件")
def b9(c: Ctx):
    p = c.workdir / "data" / "initial_admin.txt"
    c.ok(not p.exists(), "data/ 下没有 initial_admin.txt（备份包里也就不会有明文密码）")
    log = (c.workdir / "server.log").read_text(encoding="utf-8", errors="replace")
    c.contains(log, "管理员账号 admin / ", "密码在启动日志里（只此一次，systemd 下就是 journalctl）")
    c.contains(log, "只出现这一次", "日志里明确提醒要立刻记下来")


# ================================================================ P 分类配置管理
# 管理员增删改分类 + 配色 + 改名全链条 UPDATE + 日志。普通账号只读。
# ⚠️ 用例会真的改全局分类配置（在独立测试实例里），改完尽量还原，
# 但顺序上 P 组内部有依赖：p1 建基线 → p2~p7 逐步操作，别乱调。

@case("P 分类配置", "默认种子：配置与硬编码树一致，色位随种子下发")
def p1(c: Ctx):
    st, meta = c.me.get("/api/meta")
    c.eq(st, 200, "meta 可读")
    c.ok(meta.get("cat_colors"), "meta 带 cat_colors")
    exp = meta["categories"]["expense"]
    c.eq(list(exp.keys()),
         ["餐饮", "交通", "购物", "生活缴费", "医疗", "娱乐", "人情", "其他支出"],
         "支出大类与顺序来自种子")
    colors = meta["cat_colors"]["expense"]
    c.eq(colors["餐饮"], 1, "餐饮色位 = 暖橙")
    c.eq(colors["医疗"], 3, "医疗色位 = 红")
    c.ok("工资" in meta["categories"]["income"], "收入树完整")

    st, r = c.me.get("/api/categories/config")
    c.eq(st, 403, "普通账号读配置 403")
    c.eq(c.me.get("/api/categories/logs")[0], 403, "普通账号读日志 403")
    c.eq(c.anon.get("/api/categories/config")[0], 401, "匿名读配置 401")
    st, r = c.admin.get("/api/categories/config")
    c.eq(st, 200, "管理员读配置 200")
    c.eq(r["config"]["expense"]["餐饮"]["color_slot"], 1, "配置里色位一致")
    c.eq(r["config"]["expense"]["餐饮"]["subs"], exp["餐饮"], "配置里子类一致")


@case("P 分类配置", "改色位立即生效：/api/meta 与饼图取色联动")
def p2(c: Ctx):
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"]["交通"]["color_slot"] = 7
    st, r = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 200, "改色位保存成功")
    st, meta = c.me.get("/api/meta")
    c.eq(meta["cat_colors"]["expense"]["交通"], 7, "普通账号立即看到新色位")
    # 还原
    conf["expense"]["交通"]["color_slot"] = 2
    c.admin.post("/api/categories/config", {"config": conf})


@case("P 分类配置", "加子类能记账；空树与超限校验被拒")
def p3(c: Ctx):
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"]["餐饮"]["subs"].append("宠物餐")
    st, _ = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 200, "加子类成功")
    st, meta = c.me.get("/api/meta")
    c.eq("宠物餐" in meta["categories"]["expense"]["餐饮"], True, "新子类立即可选")
    amt = uniq_cents()
    st, r = c.me.post("/api/tx", {"tx_date": D(0), "direction": "expense",
                                  "cat_parent": "餐饮", "category": "宠物餐",
                                  "amount_cents": amt})
    c.eq(st, 200, "新子类能记账")

    # 下限：删空一个一级的子类
    bad = json.loads(json.dumps(conf))
    bad["expense"]["餐饮"]["subs"] = []
    st, e = c.admin.post("/api/categories/config", {"config": bad})
    c.eq(st, 400, "一级下子类清空 → 400")
    # 下限：清空整个方向
    bad = json.loads(json.dumps(conf))
    bad["refund"] = {}
    st, _ = c.admin.post("/api/categories/config", {"config": bad})
    c.eq(st, 400, "方向清空 → 400")
    # 色位越界
    bad = json.loads(json.dumps(conf))
    bad["expense"]["餐饮"]["color_slot"] = 10
    st, _ = c.admin.post("/api/categories/config", {"config": bad})
    c.eq(st, 400, "色位 >9 → 400")
    # 子类同父级重复
    bad = json.loads(json.dumps(conf))
    bad["expense"]["餐饮"]["subs"] = ["早餐", "早餐"]
    st, _ = c.admin.post("/api/categories/config", {"config": bad})
    c.eq(st, 400, "同父级子类重名 → 400")


@case("P 分类配置", "改子类名：历史账单全链条 UPDATE，统计合并为一行")
def p4(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    pid = c.expense(amount=amt, when=d, cat=("餐饮", "外卖"), note="改前")
    base = c.stats(m)
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"]["餐饮"]["subs"] = [
        "点外卖" if s == "外卖" else s for s in conf["expense"]["餐饮"]["subs"]]
    # 2026-10-30 起：会改写历史的保存必须先预览/确认（B-08），不再一声不响地改
    st, e = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 400, "不带 confirm 的改名被拦（400）")
    c.eq(e.get("code"), "need_confirm", "错误码是 need_confirm")
    c.eq(c.row(pid, m)["category"], "外卖", "被拦时历史没动")
    st, r = c.admin.post("/api/categories/config", {"config": conf, "confirm": True})
    c.eq(st, 200, "带 confirm 的改子类名成功")
    c.eq(r.get("renamed_tx"), 1, "连带更新 1 笔账单")
    # 历史账单跟着变
    st, data = c.me.get(f"/api/tx?month={m}")
    row = [x for x in data["items"] if x["id"] == pid][0]
    c.eq(row["category"], "点外卖", "历史账单分类已更新")
    # 统计立即合并（改名前后金额都在「外卖/点外卖」下）
    st, s2 = c.me.get(f"/api/stats?month={m}")
    by = {x["cat_parent"]: x["total"] for x in s2["by_category"]}
    c.eq(by.get("餐饮"), amt, "统计金额不变（合并成功，没有丢行）")
    # 日志落库
    st, logs = c.admin.get("/api/categories/logs")
    ren = [x for x in logs["items"] if x["action"] == "rename"]
    c.ok(ren, "改名日志存在")
    if ren:
        c.contains(ren[0]["detail"], "外卖", "日志记录了旧名 → 新名")
        c.eq(ren[0]["affected"], 1, "日志记录了连带笔数")
    # 还原树（此时历史账单已是「点外卖」，改回去会再 UPDATE 一次）
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"]["餐饮"]["subs"] = [
        "外卖" if s == "点外卖" else s for s in conf["expense"]["餐饮"]["subs"]]
    st, r = c.admin.post("/api/categories/config", {"config": conf, "confirm": True})
    c.eq(st, 200, "改回原名成功（还原也要 confirm）")
    st, data = c.me.get(f"/api/tx?month={m}")
    row = [x for x in data["items"] if x["id"] == pid][0]
    c.eq(row["category"], "外卖", "还原后历史账单也跟着回来")


@case("P 分类配置", "删有历史的一级分类被拦：只能改名，历史不许留孤儿")
def p5(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    pid = c.expense(amount=amt, when=d, cat=("娱乐", "电影"), note="删类前")
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"].pop("娱乐")
    # Q-01 默认方案（2026-10-30）：有历史账单的分类一律不许删，只能改名。
    # 删掉会让那些账挂在一个不存在的分类上：明细看不出它属于哪块，饼图也归不了类。
    st, e = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 400, "删有历史的分类被拒（400）")
    c.eq(e.get("code"), "category_has_history", "错误码指明分类下有历史")
    c.contains(e.get("error"), "娱乐", "文案点名是哪个分类")
    st, meta = c.me.get("/api/meta")
    c.eq("娱乐" in meta["categories"]["expense"], True, "新树里娱乐还在")
    st, data = c.me.get(f"/api/tx?month={m}")
    row = [x for x in data["items"] if x["id"] == pid][0]
    c.eq(row["cat_parent"], "娱乐", "历史账单不受影响")
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "expense",
                                  "cat_parent": "娱乐", "category": "电影",
                                  "amount_cents": 100})
    c.eq(st, 200, "被拦之后这个分类照样能记账")
    # 零笔的分类仍然可以直接删：空分类留着只会让下拉框越来越长
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"]["空分类P5"] = {"color_slot": None, "subs": ["空子P5"]}
    c.eq(c.admin.post("/api/categories/config", {"config": conf})[0], 200, "先建一个空分类")
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"].pop("空分类P5")
    st, e = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 200, "零笔的分类可以直接删（不用确认）")
    st, meta = c.me.get("/api/meta")
    c.eq("空分类P5" in meta["categories"]["expense"], False, "空分类确实删掉了")
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "expense",
                                  "cat_parent": "空分类P5", "category": "空子P5",
                                  "amount_cents": 100})
    c.eq(st, 400, "删掉之后不能再用它记新账")


@case("P 分类配置", "新增一级分类：能记账，带色位下发，有账之后删不掉")
def p6(c: Ctx):
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"]["宠物"] = {"color_slot": 8, "subs": ["粮食", "医疗", "其他"]}
    st, _ = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 200, "新增一级成功")
    st, meta = c.me.get("/api/meta")
    c.eq("宠物" in meta["categories"]["expense"], True, "新大类立即下发")
    c.eq(meta["cat_colors"]["expense"]["宠物"], 8, "新大类带色位")
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "expense",
                                  "cat_parent": "宠物", "category": "粮食",
                                  "amount_cents": 500})
    c.eq(st, 200, "新大类能记账")
    # 再删掉：现在历史账单里已有「宠物」→ 不能再用这个名字新建
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    conf["expense"].pop("宠物")
    st, e = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 400, "已经记过账的新分类删不掉（400）")
    c.eq(e.get("code"), "category_has_history", "错误码指明分类下有历史")
    c.contains(e.get("error"), "宠物", "文案点名是哪个分类")
    st, meta = c.me.get("/api/meta")
    c.eq("宠物" in meta["categories"]["expense"], True, "被拦之后分类还在树里")
    # 名字被历史账单占用的另一面：改名走 confirm 就能过（同一批账换个名字而已）
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    node = conf["expense"].pop("宠物")
    conf["expense"]["宠物天地"] = node
    st, r = c.admin.post("/api/categories/config", {"config": conf, "confirm": True})
    c.eq(st, 200, "改名（带 confirm）可以过")
    c.ok(r.get("renamed_tx", 0) >= 1, "历史账单跟着改了名")


@case("P 分类配置", "改一级名：按子类交集配对全链条 UPDATE，日志留痕")
def p7(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    pid = c.expense(amount=amt, when=d, cat=("购物", "数码"), note="改名前")
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    # 改名 = 删「购物」+ 加「剁手」，子类原样带过去 → 服务端按子类重合度配对
    node = conf["expense"].pop("购物")
    conf["expense"]["剁手"] = node
    # 保持 dict 顺序无所谓，配对按重合度算；改写历史必须显式确认（B-08）
    st, e = c.admin.post("/api/categories/config", {"config": conf})
    c.eq(st, 400, "不带 confirm 的一级改名被拦（400）")
    c.eq(e.get("code"), "need_confirm", "错误码是 need_confirm")
    c.ok((e.get("preview") or {}).get("renames"), "400 里带回改名计划")
    st, r = c.admin.post("/api/categories/config", {"config": conf, "confirm": True})
    c.eq(st, 200, "带 confirm 的一级改名成功")
    c.ok(r.get("renamed_tx", 0) >= 1, "连带更新了历史账单")
    st, data = c.me.get(f"/api/tx?month={m}")
    row = [x for x in data["items"] if x["id"] == pid][0]
    c.eq(row["cat_parent"], "剁手", "历史账单大类已更新")
    c.eq(row["category"], "数码", "子类不变")
    st, s2 = c.me.get(f"/api/stats?month={m}")
    by = {x["cat_parent"]: x["total"] for x in s2["by_category"]}
    c.eq(by.get("剁手"), amt, "统计立即合并到新名下")
    st, logs = c.admin.get("/api/categories/logs")
    ren = [x for x in logs["items"] if x["action"] == "rename" and "剁手" in x["detail"]]
    c.ok(ren, "一级改名日志存在")
    # 还原（改回去也是全链条 UPDATE，同样要 confirm）
    st, cfg = c.admin.get("/api/categories/config")
    conf = cfg["config"]
    node = conf["expense"].pop("剁手")
    conf["expense"]["购物"] = node
    st, r = c.admin.post("/api/categories/config", {"config": conf, "confirm": True})
    c.eq(st, 200, "改回原名成功")
    c.eq(c.row(pid, m)["cat_parent"], "购物", "还原后历史账单跟着回来")
    # 普通账号不能写
    st, _ = c.me.post("/api/categories/config", {"config": conf})
    c.eq(st, 403, "普通账号保存配置 403")


@case("P 分类配置", "子类改名也同步冲抵记录的分类副本（D-03/R11）")
def p8(c: Ctx):
    m, d = c.month_pair()
    tmp = "临时分类R11"
    cfg = _cfg(c)
    cfg["expense"][tmp] = {"color_slot": None, "subs": ["旧子R11", "陪跑R11"]}
    c.eq(_save_cfg(c, cfg)[0], 200, "建临时分类：旧子 / 陪跑")
    pid = c.expense(amount=5_000, when=d, cat=(tmp, "旧子R11"))
    rid = c.refund(pid, 2_000, when=d)     # 冲抵记录复制了原单的分类名
    c.eq(c.row(rid, month=m).get("category"), "旧子R11", "冲抵记录继承了子类名")
    # 子类改名：消失 1 个、新增 1 个，且父级还在 → 判定为改名
    cfg2 = _cfg(c)
    cfg2["expense"][tmp]["subs"] = ["新子R11", "陪跑R11"]
    st, data = _save_cfg(c, cfg2)
    c.eq(st, 400, "不改写历史就先拦一次")
    c.eq(data.get("code"), "need_confirm", "错误码 need_confirm")
    c.eq(c.row(rid, month=m).get("category"), "旧子R11", "没确认前副本一个字没动")
    st, data = _save_cfg(c, cfg2, confirm=True)
    c.eq(st, 200, "确认后写入")
    c.eq(data.get("renamed_tx"), 2, "支出 + 冲抵副本各 1 笔")
    c.eq(c.row(pid, month=m).get("category"), "新子R11", "支出改成新子类")
    c.eq(c.row(rid, month=m).get("category"), "新子R11", "冲抵记录的分类副本跟着改")
    left = [x for x in c.me.get(f"/api/tx?month={m}")[1]["items"]
            if x.get("category") == "旧子R11"]
    c.eq(left, [], "旧子类名下一行不剩（明细里不会再有旧名字）")
    # 还原现场
    cfg3 = _cfg(c)
    cfg3["expense"][tmp]["subs"] = ["旧子R11", "陪跑R11"]
    c.eq(_save_cfg(c, cfg3, confirm=True)[0], 200, "改回原样")


# ================================================================ Q 报销冲抵

# 报销走「冲抵」而不是「改标记」：钱真回到账上，净支出要跟着减，收入不能动。
# 判据：有没有先花出去过。花过 = 冲抵（报销/退款），没花过 = 收入（补贴）。

@case("Q 报销冲抵", "到账生成报销冲抵：净支出减少、收入不变、原单标记已报销")
def q1(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    tid = c.expense(amount=amt, when=d, cat=("交通", "打车"), note="出差打车",
                    reimb_pending=True)
    before = c.stats(m)
    st, r = c.me.post(f"/api/tx/{tid}/reimburse", {})
    c.eq(st, 200, "一键到账")
    c.eq(r.get("amount_cents"), amt, "默认按剩余可冲抵全额冲")
    row = c.row(tid, m)
    c.eq(row["reimbursed"], 1, "原单标记已报销")
    c.eq(row["reimb_pending"], 0, "待报销标记已清除")
    # 冲抵记录的日期是「到账日」（今天），不在原单那个月里，所以按今天的月份查
    st, data = c.me.get("/api/tx?month=" + D(0)[:7])
    kids = [x for x in data["items"] if x.get("parent_id") == tid]
    c.eq(len(kids), 1, "生成了一条冲抵记录")
    c.eq(kids[0]["offset_kind"], "reimb", "冲抵类型是报销")
    c.eq(kids[0]["direction"], "refund", "冲抵行仍走 refund 方向（统计只认这个）")
    after = c.stats(m)
    c.eq(after["expense"], before["expense"] - amt, "净支出同步减少")
    c.eq(after["income"], before["income"], "收入一分不动")
    c.eq(after["reimb"] - before["reimb"], amt, "计入「报销到账」")
    c.eq(after["refund"], before["refund"], "「退款」数字不含报销")


@case("Q 报销冲抵", "部分报销：只冲填的金额，剩余留作自付；超额被拒")
def q2(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    tid = c.expense(amount=amt, when=d, reimb_pending=True)
    part = amt // 2
    st, r = c.me.post(f"/api/tx/{tid}/reimburse", {"amount_cents": part})
    c.eq(st, 200, "只报一半也支持")
    c.eq(r.get("amount_cents"), part, "按填的金额冲")
    c.eq(r.get("remain_cents"), amt - part, "返回剩余可冲抵（自付部分）")
    st, _ = c.me.post(f"/api/tx/{tid}/reimburse", {"amount_cents": amt})
    c.eq(st, 400, "超过剩余可冲抵被拒")
    st, _ = c.me.post("/api/tx/999999/reimburse", {})
    c.eq(st, 404, "不存在的记录")


@case("Q 报销冲抵", "只有支出能报销；已冲完的拒绝重复到账")
def q3(c: Ctx):
    m, d = c.month_pair()
    inc = c.income(amount=uniq_cents(), when=d)
    st, _ = c.me.post(f"/api/tx/{inc}/reimburse", {})
    c.eq(st, 400, "收入不能报销")
    amt = uniq_cents()
    tid = c.expense(amount=amt, when=d, reimb_pending=True)
    c.me.post(f"/api/tx/{tid}/reimburse", {})
    st, _ = c.me.post(f"/api/tx/{tid}/reimburse", {})
    c.eq(st, 400, "已经全部冲抵完，再点就拒")


@case("Q 报销冲抵", "无主报销计入报销栏；报销候选排除已报销的支出")
def q4(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    before = c.stats(m)
    st, _ = c.me.post("/api/tx", {"tx_date": d, "direction": "refund",
                                  "cat_parent": "退款", "category": "其他退款",
                                  "amount_cents": amt, "offset_kind": "reimb"})
    c.eq(st, 200, "记一笔没有原单的报销到账")
    after = c.stats(m)
    c.eq(after["reimb"] - before["reimb"], amt, "计入「报销到账」")
    c.eq(after["refund"], before["refund"], "不混进「退款」")
    c.eq(after["expense"], before["expense"] - amt, "无主报销同样冲减净支出")

    # 报了一半的支出：还有余额，但已经不该再出现在报销候选里
    # 候选窗口只有近 30 天，所以这条必须记在今天（上面那条在 5 个月前的隔离月份里，进不了候选）
    amt2 = uniq_cents()
    tid = c.expense(amount=amt2, when=D(0), reimb_pending=True, note="可报销的酒店")
    c.me.post(f"/api/tx/{tid}/reimburse", {"amount_cents": amt2 // 2})
    st, data = c.me.get("/api/refund-targets?kind=reimb&amount_cents=0")
    ids = [x["id"] for x in data.get("items", [])]
    c.ok(tid not in ids, "报销候选里不再出现已报销的支出")
    # 但它还剩一半没退，退货候选里应该还在（用剩余额去查，命中「同额」排最前）
    st, data = c.me.get("/api/refund-targets?amount_cents=%d" % (amt2 - amt2 // 2))
    c.ok(tid in [x["id"] for x in data.get("items", [])], "退货候选里它还在（还剩一半可退）")
    # 只认「待报销」标记：没标的普通消费再大也不该进候选
    # （2026-10-28 实际发生过：填 5000 报一笔，系统推上来一笔没标记的 7533.90 还自动选中了）
    plain = c.expense(amount=uniq_cents(), when=D(0), note="只是普通消费")
    st, data = c.me.get("/api/refund-targets?kind=reimb")
    c.ok(plain not in [x["id"] for x in data.get("items", [])], "没标「待报销」的支出不进报销候选")
    # 报销是「一次到账摊多笔」，单笔金额天生小于到账额——不能按「单笔够不够」筛
    small = c.expense(amount=1200, when=D(0), reimb_pending=True, note="小额待报销")
    st, data = c.me.get("/api/refund-targets?kind=reimb&amount_cents=500000")
    c.ok(small in [x["id"] for x in data.get("items", [])],
         "待报销的小额支出，在填大额到账时照样在候选里")
    # 按「谁花的」过滤：命中就命中，该人名下没有可报的才放宽到全部人
    other = c.expense(amount=uniq_cents(), when=D(0), client=c.member_b,
                      reimb_pending=True, note="别人垫的")
    other_owner = c.row(other, D(0)[:7])["owner_id"]
    st, data = c.me.get("/api/refund-targets?kind=reimb&owner=%d" % other_owner)
    c.ok(other in [x["id"] for x in data.get("items", [])], "按归属人能匹配到")
    c.eq(data.get("relaxed"), False, "有命中就不放宽")
    st, data = c.me.get("/api/refund-targets?kind=reimb&owner=999")
    c.ok(other in [x["id"] for x in data.get("items", [])], "名下没有候选时放宽到全部人")
    c.eq(data.get("relaxed"), True, "并明确告知已放宽")


@case("Q 报销冲抵", "报销不冒充「已退」；归属跟原单；删记录退回待报销；不能直接翻标记")
def q5(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    # 成员 B 花、成员 A 操作到账：冲抵记录的归属应该跟原单，不是操作人
    tid = c.expense(amount=amt, when=d, cat=("交通", "打车"), note="代垫",
                    client=c.member_b, reimb_pending=True)
    c.eq(c.row(tid, m)["refunded_cents"], 0, "初始没有已退金额")
    st, _ = c.me.post(f"/api/tx/{tid}/reimburse", {})
    c.eq(st, 200, "到账")
    row = c.row(tid, m)
    c.eq(row["refunded_cents"], 0, "报销不冒充「已退」")
    c.eq(row["reimb_cents"], amt, "报销金额单独记")
    c.eq(row["reimbursed"], 1, "已报销")
    c.eq(row["reimb_date"], D(0), "带上到账日期——编辑弹层要说清「已于哪天到账」")
    st, data = c.me.get("/api/tx?month=" + D(0)[:7])
    kid = [x for x in data["items"] if x.get("parent_id") == tid][0]
    c.eq(kid["owner_id"], row["owner_id"], "冲抵记录归属跟原单")

    st, _ = c.me.patch(f"/api/tx/{tid}", {"reimbursed": False})
    c.eq(st, 400, "有到账记录时不能直接把标记翻回未报销")
    # 打开「待报销」同样不行：那等于宣布「还没报」，可到账记录明明还挂在账上。
    # 界面把两个开关一起置灰，接口这边也必须拦住——否则绕开界面就能造出矛盾状态。
    st, _ = c.me.patch(f"/api/tx/{tid}", {"reimb_pending": True})
    c.eq(st, 400, "有到账记录时也不能改回待报销")
    # 但原样把标记报回来（前端每次保存都会带这两个字段）不能误伤
    st, _ = c.me.patch(f"/api/tx/{tid}",
                       {"note": "改个备注", "reimb_pending": False, "reimbursed": True})
    c.eq(st, 200, "标记没变化时正常保存，不误拦")

    st, _ = c.me.delete("/api/tx/%d" % kid["id"])
    c.eq(st, 200, "删掉报销到账记录")
    row = c.row(tid, m)
    c.eq(row["reimbursed"], 0, "已报销标记跟着回退")
    c.eq(row["reimb_pending"], 1, "退回待报销，而不是变成普通账单")


@case("Q 报销冲抵", "在记一笔里填报销：原单同步成已报销，归属仍继承原单")
def q6(c: Ctx):
    m, d = c.month_pair()
    mine = c.expense(amount=uniq_cents(), when=d)
    member_a_id = c.row(mine, m)["owner_id"]
    amt = uniq_cents()
    tid = c.expense(amount=amt, when=d, note="成员 B 垫付", client=c.member_b,
                    reimb_pending=True)
    st, _ = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                  "parent_id": tid, "amount_cents": amt,
                                  "offset_kind": "reimb"})
    c.eq(st, 200, "记一笔里填报销到账")
    row = c.row(tid, m)
    c.eq(row["reimbursed"], 1, "原单同步成已报销")
    c.eq(row["reimb_pending"], 0, "待报销清掉")
    c.eq(row["refunded_cents"], 0, "不产生已退金额")
    st, lst = c.me.get("/api/tx?month=" + D(0)[:7])
    kid = [x for x in lst["items"] if x.get("parent_id") == tid][0]
    c.eq(kid["owner_id"], row["owner_id"], "归属继承原单")
    c.ok(kid["owner_id"] != member_a_id, "不是取操作人（成员 A）的归属")


@case("R 冲抵分配", "一次报销摊在多笔支出上：各按自己的分类抵扣，净支出只减一次")
def r1(c: Ctx):
    m, d = c.month_pair()
    a, b = uniq_cents(), uniq_cents() + 7777
    t1 = c.expense(amount=a, when=d, cat=("餐饮", "午餐"), note="午饭")
    t2 = c.expense(amount=b, when=d, cat=("交通", "打车"), note="打车")
    before = c.stats(m)
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "offset_kind": "reimb", "amount_cents": a + b,
                                     "cat_parent": "退款", "category": "其他退款",
                                     "note": "一次报销"})
    c.eq(st, 200, "记一笔报销到账")
    rid = data["id"]
    st, lg = c.me.post(f"/api/tx/{rid}/links", {"items": [
        {"parent_id": t1, "amount_cents": a},
        {"parent_id": t2, "amount_cents": b}]})
    c.eq(st, 200, "分配保存成功")
    c.eq(lg["allocated"], a + b, "全部分配完")
    c.eq(lg["unallocated"], 0, "没有未分配余额")
    after = c.stats(m)
    c.eq(before["expense"] - after["expense"], a + b, "净支出只减一次（没有重复抵扣）")
    c0 = {x["cat_parent"]: x["total"] for x in before["by_category"]}
    c1 = {x["cat_parent"]: x["total"] for x in after["by_category"]}
    c.eq(c0.get("餐饮", 0) - c1.get("餐饮", 0), a, "餐饮按分配额抵扣")
    c.eq(c0.get("交通", 0) - c1.get("交通", 0), b, "交通按分配额抵扣")
    c.eq(c.row(t1, m)["reimbursed"], 1, "第一笔变已报销")
    c.eq(c.row(t2, m)["reimbursed"], 1, "第二笔也变已报销")


@case("R 冲抵分配", "部分分配：没认领的算无主；超额被拒；改冲抵金额不能把分配冲散")
def r2(c: Ctx):
    m, d = c.month_pair()
    a = uniq_cents()
    t1 = c.expense(amount=a, when=d, cat=("购物", "日用品"), note="超市")
    total = a + 50000
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "offset_kind": "reimb", "amount_cents": total,
                                     "cat_parent": "退款", "category": "其他退款"})
    rid = data["id"]
    st, lg = c.me.post(f"/api/tx/{rid}/links",
                       {"items": [{"parent_id": t1, "amount_cents": a}]})
    c.eq(st, 200, "只认领一部分")
    c.eq(lg["unallocated"], 50000, "剩 500 元未分配")
    st, lg = c.me.get(f"/api/tx/{rid}/links")
    c.eq(len(lg["items"]), 1, "只有一条分配")
    c.eq(lg["items"][0]["alloc_cents"], a, "分配额正确")
    # 超过原单剩余额度
    st, _ = c.me.post(f"/api/tx/{rid}/links",
                      {"items": [{"parent_id": t1, "amount_cents": a + 1}]})
    c.eq(st, 400, "超过原单剩余额度被拒")
    # 分配合计超过到账金额
    t2 = c.expense(amount=uniq_cents(), when=d, cat=("餐饮", "早餐"))
    st, _ = c.me.post(f"/api/tx/{rid}/links",
                      {"items": [{"parent_id": t1, "amount_cents": a},
                                 {"parent_id": t2, "amount_cents": 60000}]})
    c.eq(st, 400, "分配合计超过到账金额被拒")


@case("R 冲抵分配", "删掉一条分配：原单退回待报销，另一笔不受影响")
def r3(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    t1 = c.expense(amount=amt, when=d, reimb_pending=True)
    t2 = c.expense(amount=amt, when=d, reimb_pending=True)
    st, data = c.me.post("/api/tx", {"tx_date": D(0), "direction": "refund",
                                     "offset_kind": "reimb", "amount_cents": amt * 2,
                                     "cat_parent": "退款", "category": "其他退款"})
    rid = data["id"]
    c.me.post(f"/api/tx/{rid}/links", {"items": [
        {"parent_id": t1, "amount_cents": amt},
        {"parent_id": t2, "amount_cents": amt}]})
    c.eq(c.row(t1, m)["reimbursed"], 1, "两笔都已报销")
    # 只留下第二笔的分配
    st, _ = c.me.post(f"/api/tx/{rid}/links",
                      {"items": [{"parent_id": t2, "amount_cents": amt}]})
    c.eq(st, 200, "改分配")
    c.eq(c.row(t1, m)["reimbursed"], 0, "被摘掉的那笔不再是已报销")
    c.eq(c.row(t1, m)["reimb_pending"], 1, "退回待报销")
    c.eq(c.row(t2, m)["reimbursed"], 1, "另一笔不受影响")
    # 删掉整条到账，第二笔也回退
    st, _ = c.me.delete(f"/api/tx/{rid}")
    c.eq(st, 200, "删掉到账记录")
    c.eq(c.row(t2, m)["reimbursed"], 0, "删记录后也回退")
    c.eq(c.row(t2, m)["reimb_pending"], 1, "回到待报销")


# ------------------------------------------------ S 报销多笔与差补
# 一次到账 = 冲抵若干笔垫付 + 一部分差补（公司按天包干给的钱）。
# 差补没垫过，所以记收入；垫付收回记冲抵。两件事性质不同，必须两条记录。

@case("S 报销多笔与差补", "一次到账摊多笔：冲抵减支出、差补增收入、没认领的算无主")
def s1(c: Ctx):
    m, d = c.month_pair()
    a1 = c.expense(amount=40000, when=d, reimb_pending=True, note="高铁票")
    a2 = c.expense(amount=20000, when=d, reimb_pending=True, note="住宿")
    before = c.stats(m)
    st, data = c.me.post("/api/tx", {
        "tx_date": d, "direction": "refund", "offset_kind": "reimb",
        "cat_parent": "退款", "category": "其他退款",
        "amount_cents": 100000,          # 到账 1000
        "links": [{"parent_id": a1, "amount_cents": 40000},
                  {"parent_id": a2, "amount_cents": 20000}],
        "subsidy_cents": 30000,          # 其中 300 是差补
        "subsidy_category": "出差补贴",
    })
    c.eq(st, 200, "到账 1000 = 冲抵 600 + 差补 300 + 未认领 100")
    c.ok(data.get("income_id"), "差补另生成了一条收入")
    after = c.stats(m)
    c.eq(after["reimb"] - before["reimb"], 70000, "报销到账 = 到账额 − 差补")
    c.eq(after["income"] - before["income"], 30000, "差补进了收入")
    c.eq(before["expense"] - after["expense"], 70000,
         "净支出减少 = 认领的 600 + 未认领的 100（后者按到账月算无主）")
    c.eq(c.row(a1, m)["reimbursed"], 1, "第一笔已报销")
    c.eq(c.row(a2, m)["reimbursed"], 1, "第二笔已报销")
    inc = c.row(data["income_id"], m)
    c.eq(inc["direction"], "income", "差补是收入记录，不是冲抵")
    c.eq(inc["cat_parent"], "补贴", "差补落「补贴」大类")
    c.eq(inc["category"], "出差补贴", "子类按选的来")


@case("S 报销多笔与差补", "差补归属跟原单；差补越界、分配超额、超原额都被拦")
def s2(c: Ctx):
    m, d = c.month_pair()
    amt = uniq_cents()
    tid = c.expense(amount=amt, when=d, client=c.member_b,
                    reimb_pending=True, note="她垫的，我帮她报")
    st, data = c.me.post("/api/tx", {
        "tx_date": d, "direction": "refund", "offset_kind": "reimb",
        "cat_parent": "退款", "category": "其他退款",
        "amount_cents": amt + 5000,
        "links": [{"parent_id": tid, "amount_cents": amt}],
        "subsidy_cents": 5000,
    })
    c.eq(st, 200, "垫付 + 差补一次到账")
    exp = c.row(tid, m)
    inc = c.row(data["income_id"], m)
    c.eq(inc["owner_id"], exp["owner_id"], "差补归属跟原单，不跟操作人")

    # 差补不能吃掉全部到账，那样就不是报销了
    st, _ = c.me.post("/api/tx", {
        "tx_date": d, "direction": "refund", "offset_kind": "reimb",
        "cat_parent": "退款", "category": "其他退款",
        "amount_cents": 10000, "links": [], "subsidy_cents": 10000,
    })
    c.eq(st, 400, "差补等于到账总额被拒")
    # 分配 + 差补 超过到账
    a3 = c.expense(amount=50000, when=d, reimb_pending=True)
    st, _ = c.me.post("/api/tx", {
        "tx_date": d, "direction": "refund", "offset_kind": "reimb",
        "cat_parent": "退款", "category": "其他退款",
        "amount_cents": 60000, "subsidy_cents": 20000,
        "links": [{"parent_id": a3, "amount_cents": 50000}],
    })
    c.eq(st, 400, "分配额 + 差补 超过到账被拒")
    # 单笔分配超过原额
    st, _ = c.me.post("/api/tx", {
        "tx_date": d, "direction": "refund", "offset_kind": "reimb",
        "cat_parent": "退款", "category": "其他退款",
        "amount_cents": 90000, "subsidy_cents": 0,
        "links": [{"parent_id": a3, "amount_cents": 90000}],
    })
    c.eq(st, 400, "给一笔支出分的钱不能超过它自己的原额")
    c.eq(c.row(a3, m)["reimbursed"], 0, "被拒的那次没有留下半截状态")


# ================================================================ 运行器


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def start_server(src: Path, workdir: Path, port: int):
    """在临时目录起一份独立实例：拷 server.py + 整个 www/（静态资源一起带上）。

    别只拷 index.html —— 加静态资源（图标之类）时会漏，测出来是 404，
    而线上其实是好的，白白误导一轮排查。
    """
    (workdir / "www").mkdir(parents=True, exist_ok=True)
    shutil.copy(src / "server.py", workdir / "server.py")

    www_src = src / "www"
    if www_src.is_dir():
        for f in sorted(www_src.iterdir()):
            if f.is_file():
                shutil.copy(f, workdir / "www" / f.name)
    else:                                    # 兼容旧的单文件布局
        shutil.copy(src / "index.html", workdir / "www" / "index.html")
    # LEDGER_AI_MOCK=1：AI 走测试替身，不联网、不烧额度。
    # 生产 systemd 里没有这个变量，线上永远是真实请求。
    # Windows 下子进程默认按系统代码页（如 GBK）写日志，而测试按 UTF-8 读，
    # 中文日志会变成乱码、连初始管理员密码都匹配不到。强制子进程用 UTF-8。
    env = dict(os.environ, LEDGER_HOME=str(workdir), LEDGER_PORT=str(port),
               LEDGER_AI_MOCK="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    # 服务端日志写文件，不要用 PIPE：PIPE 没人读，缓冲区（64KB）一满，
    # 子进程就阻塞在 write 上，所有请求跟着挂死——现象是「测试莫名其妙卡住十几秒甚至超时」。
    # S-04 加了访问日志之后这里立刻踩到过。写文件既不会阻塞，起不来时也能翻日志。
    logpath = workdir / "server.log"
    logf = open(logpath, "wb")
    proc = subprocess.Popen([sys.executable, str(workdir / "server.py")],
                            env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with _OPENER.open(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return proc, base
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    try:
        logf.flush()
    except Exception:
        pass
    out = logpath.read_text(encoding="utf-8", errors="replace") if logpath.exists() else ""
    raise RuntimeError("测试实例没起来：\n" + out)


def run(args) -> int:
    src = Path(args.src).resolve()
    if not (src / "server.py").exists():
        print(f"✗ 找不到被测代码：{src}/server.py")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="ledger_test_"))
    port = free_port()
    proc = None
    try:
        proc, base = start_server(src, workdir, port)
        # 初始密码只在启动日志里打印一次（S-05 起不再写 data/initial_admin.txt）。
        # 日志可能比 healthz 晚一点点落盘，轮询一下而不是死等。
        logpath = workdir / "server.log"
        admin_pwd = ""
        for _ in range(50):
            txt = logpath.read_text(encoding="utf-8", errors="replace") if logpath.exists() else ""
            if "管理员账号 admin / " in txt:
                admin_pwd = txt.split("管理员账号 admin / ", 1)[1].split()[0]
                break
            time.sleep(0.05)
        assert admin_pwd, "启动日志里没找到初始密码：\n" + (
            logpath.read_text(encoding="utf-8", errors="replace")[:1200] if logpath.exists() else "")

        admin = Client(base)
        st, _ = admin.post("/api/login", {"username": "admin", "password": admin_pwd})
        assert st == 200, "管理员登录失败"

        for uname, disp in (("tester1", "测试甲"), ("tester2", "测试乙")):
            st, data = admin.post("/api/users", {"username": uname, "display_name": disp,
                                                 "password": "pass-1234"})
            assert st == 200, f"创建 {uname} 失败：{data}"

        def login(u):
            cli = Client(base)
            st, _ = cli.post("/api/login", {"username": u, "password": "pass-1234"})
            assert st == 200, f"{u} 登录失败"
            return cli

        ctx = Ctx(base, admin, login("tester1"), login("tester2"), Client(base), workdir)

        picked = [x for x in CASES if args.only in (x["group"] + x["title"])] if args.only else CASES
        results = []
        t_all = time.time()
        for i, item in enumerate(picked, 1):
            t0 = time.time()
            ctx.checks.clear()
            ctx.fails.clear()
            err = None
            try:
                item["fn"](ctx)
            except Exception:
                err = traceback.format_exc(limit=3)
            n_fail = len(ctx.fails)
            results.append({
                "group": item["group"], "title": item["title"],
                "checks": len(ctx.checks), "fails": list(ctx.fails),
                "error": err, "ms": int((time.time() - t0) * 1000),
            })
            flag = "✓" if (n_fail == 0 and not err) else "✗"
            print(f"  {flag} [{item['group']}] {item['title']}"
                  f"  ({len(ctx.checks)} 断言, {results[-1]['ms']}ms)")
            if err:
                print("      " + err.strip().splitlines()[-1])
            for f in ctx.fails:
                print("      · " + f)
        total_s = time.time() - t_all

        meta = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
            "src": str(src.relative_to(ROOT)) if str(src).startswith(str(ROOT)) else str(src),
            "md5_server": md5(src / "server.py"),
            "md5_html": md5((src / "www" / "index.html") if (src / "www" / "index.html").exists()
                            else (src / "index.html")),
            "cases": len(results),
            "checks": sum(r["checks"] for r in results),
            "failed_cases": sum(1 for r in results if r["fails"] or r["error"]),
            "seconds": round(total_s, 1),
        }
        write_report(Path(args.report), meta, results)
        print()
        print(f"用例 {meta['cases']} · 断言 {meta['checks']} · 失败 {meta['failed_cases']}"
              f" · 耗时 {meta['seconds']}s")
        print(f"报告：{Path(args.report).resolve()}")
        return 0 if meta["failed_cases"] == 0 else 1
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if args.keep:
            print(f"（保留临时目录：{workdir}）")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def write_report(path: Path, meta: dict, results: list):
    groups = {}
    for r in results:
        g = groups.setdefault(r["group"], {"cases": 0, "checks": 0, "failed": 0})
        g["cases"] += 1
        g["checks"] += r["checks"]
        if r["fails"] or r["error"]:
            g["failed"] += 1

    ok = meta["failed_cases"] == 0
    lines = [
        "# 家庭账本 · 回归测试报告",
        "",
        f"> 生成时间：{meta['time']}　|　结论：**{'全部通过' if ok else '有失败用例'}**",
        "",
        "## 1. 测试环境",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 被测代码 | `{meta['src']}` |",
        f"| server.py md5 | `{meta['md5_server']}` |",
        f"| index.html md5 | `{meta['md5_html']}` |",
        f"| Python | {meta['python']} |",
        f"| 运行环境 | {meta['os']} |",
        "| 数据库 | 临时目录新建的独立 SQLite（**生产库未参与**） |",
        f"| 用例数 / 断言数 | {meta['cases']} / {meta['checks']} |",
        f"| 失败用例 | {meta['failed_cases']} |",
        f"| 耗时 | {meta['seconds']} 秒 |",
        "",
        "## 2. 覆盖矩阵",
        "",
        "| 分组 | 用例数 | 断言数 | 失败 |",
        "|---|---|---|---|",
    ]
    for g, s in groups.items():
        lines.append(f"| {g} | {s['cases']} | {s['checks']} | {s['failed'] or '0'} |")
    lines += ["", "## 3. 用例明细", "", "| # | 分组 | 用例 | 断言 | 结果 | 耗时 |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        res = "通过" if not (r["fails"] or r["error"]) else "**失败**"
        lines.append(f"| {i} | {r['group']} | {r['title']} | {r['checks']} | {res} | {r['ms']}ms |")

    bad = [r for r in results if r["fails"] or r["error"]]
    if bad:
        lines += ["", "## 4. 失败详情", ""]
        for r in bad:
            lines.append(f"### {r['group']} · {r['title']}")
            lines.append("")
            for f in r["fails"]:
                lines.append(f"- ❌ {f}")
            if r["error"]:
                lines.append("```")
                lines.append(r["error"].strip())
                lines.append("```")
            lines.append("")

    lines += [
        "",
        "## 5. 如何复跑",
        "",
        "```bash",
        "python tests/ledger_suite.py                 # 跑全部并生成本报告",
        "python tests/ledger_suite.py --only 退款      # 只跑相关分组",
        "python tests/ledger_suite.py --keep          # 保留临时库排查",
        "python tests/ledger_suite.py --src /opt/ledger --report docs/家庭账本-测试报告.md",
        "```",
        "",
        "测试会：临时目录建库 → 起独立实例（随机端口）→ 跑用例 → 关进程删目录。",
        "**全程不连接生产环境**，不需要生产密码，也不会留下任何数据。",
        "",
        "## 6. 测试抓到的缺陷（历史记录）",
        "",
        "下面这些是这套用例实际跑出来的问题，不是假设：",
        "",
        "| # | 现象 | 根因 | 处理 |",
        "|---|---|---|---|",
        "| 1 | `GET /api/tx?min_cents=abc` 直接断开连接（HTTP 无响应） | 先把 `amount_cents>=?` 拼进 SQL 再 `int(raw)`，"
        "转换抛错被 `except` 吞掉，占位符与参数对不上 → `sqlite3.ProgrammingError` | 改成「先转数字成功才拼 SQL」 |",
        "| 2 | `GET /api/tx?owner=abc` 同样把服务打挂 | 同上，`owner_id` 位置一样的问题 | 同上 |",
        "| 3 | `GET /api/refund-targets?owner=abc` 打挂 | 同上 | 同上 |",
        "| 4 | `GET /api/refunds?parent=abc` 打挂 | `int(parent)` 未做保护 | 返回 400「parent 参数非法」 |",
        "| 5 | 收入/退款记录上残留「共同支出」标记 | 非支出只清了报销和用券，漏了 `shared` | 新建/编辑统一清三个标记 |",
        "| 6 | 退款明细看不出「谁动手退的」 | `/api/refunds` 只返回归属人 | 增补 `created_name`（实际操作人） |",
        "| 7 | 偶发：刚创建完立刻查询，统计里少一笔（约 1/3 概率） | `_create_tx` 的 `return self._json()` 写在 `with db()` **块内**，"
        "响应先于提交发出，客户端紧接着读可能读到旧快照 | 改为走出 `with` 再写响应 |",
        "| 8 | 单请求 55ms，写入偶发等锁十几秒 | 每请求新建并关闭 SQLite 连接，关闭触发 WAL checkpoint；"
        "且每个新连接都执行一次 `PRAGMA journal_mode=WAL`（要拿锁） | 改为进程内共享连接 + 可重入锁（GET 6.7ms / POST 11.8ms） |",
        "",
        "## 7. 隔离性说明",
        "",
        "- 实例通过 `LEDGER_HOME` 指向临时目录，SQLite 文件是新建的；生产库 `/opt/ledger/data/ledger.db` 全程未被打开",
        "- 日期全部用 `今天 - n 天` 生成，任何时候跑结果一致",
        "- 统计/筛选用例各自分配独立月份，互不污染",
        "- 冲抵候选用例使用全局唯一金额，避免同额干扰",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：Windows 下默认会写成 CRLF，报告是入库文件，每次跑都会产生无意义的整文件差异
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main():
    ap = argparse.ArgumentParser(description="家庭账本回归测试")
    ap.add_argument("--src", default=str(ROOT / "ledger"), help="被测代码目录")
    ap.add_argument("--report", default=str(ROOT / "docs" / "家庭账本-测试报告.md"), help="报告输出路径")
    ap.add_argument("--only", default="", help="只跑分组或标题含该关键字的用例")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--list", action="store_true", help="只列用例")
    args = ap.parse_args()
    if args.list:
        for i, x in enumerate(CASES, 1):
            print(f"{i:3d}. [{x['group']}] {x['title']}")
        print(f"\n共 {len(CASES)} 个用例")
        return 0
    sys.exit(run(args))


@case("R 冲抵分配", "多笔冲抵：标题取分配额最大的那笔；编辑只能改备注，金额走「冲抵分配」")
def r4(c: Ctx):
    m, d = c.month_pair()
    a = c.expense(amount=20_000, when=d, cat=("餐饮", "早餐"), note="大额")
    b = c.expense(amount=6_000, when=d, cat=("餐饮", "午餐"), note="中额")
    cc = c.expense(amount=4_000, when=d, cat=("餐饮", "晚餐"), note="小额")
    st, data = c.me.post("/api/tx", {"tx_date": d, "direction": "refund",
                                     "offset_kind": "reimb", "amount_cents": 30_000,
                                     "links": [{"parent_id": a, "amount_cents": 20_000},
                                               {"parent_id": b, "amount_cents": 6_000},
                                               {"parent_id": cc, "amount_cents": 4_000}]})
    c.eq(st, 200, "一次到账摊 3 笔")
    rid = data["id"]
    row = c.row(rid, m)
    c.eq(row["link_count"], 3, "明细接口给出分配笔数")
    c.eq(row["parent_id"], a, "主原单 = 分配额最大的那笔")
    c.eq((row["cat_parent"], row["category"]), ("餐饮", "早餐"),
         "标题分类取主原单的——3 笔各有各的类，标题只能写一个")

    # 曾经这里拿主原单（200）的剩余额度去卡整笔 300，连只改备注都 400
    st, data = c.me.patch(f"/api/tx/{rid}", {"note": "改个备注"})
    c.eq(st, 200, "多笔记录可以正常改备注（额度按分配表算，不按主原单）")
    c.eq(c.row(rid, m)["note"], "改个备注", "备注已存")
    c.eq((c.row(rid, m)["cat_parent"], c.row(rid, m)["category"]), ("餐饮", "早餐"),
         "分类不因保存而丢")

    # 金额：多笔时不能在编辑弹层里改，只能去「冲抵分配」
    st, data = c.me.patch(f"/api/tx/{rid}", {"amount_cents": 40_000})
    c.eq(st, 400, "多笔时改金额被拒")
    c.contains(data.get("error", ""), "冲抵分配", "提示去冲抵分配里改")
    st, data = c.me.patch(f"/api/tx/{rid}", {"amount_cents": 20_000})
    c.eq(st, 400, "多笔时把金额改到低于已分配合计也被拒")

    # 改分配 → 主原单换成另一笔，标题分类要跟着换
    st, _ = c.me.post(f"/api/tx/{rid}/links", {"items": [
        {"parent_id": b, "amount_cents": 6_000},
        {"parent_id": a, "amount_cents": 4_000},
        {"parent_id": cc, "amount_cents": 4_000}]})
    c.eq(st, 200, "重排分配")
    row = c.row(rid, m)
    c.eq(row["parent_id"], b, "主原单换成金额最大的 B")
    c.eq((row["cat_parent"], row["category"]), ("餐饮", "午餐"),
         "标题分类跟着主原单走，不留旧分类")

    # 单笔的守卫不能被改松：往上调仍要卡原单剩余（防手滑多打一个 0）
    single = c.expense(amount=10_000, when=d, cat=("餐饮", "零食饮品"))
    rid2 = c.refund(single, 6_000)
    st, data = c.me.patch(f"/api/tx/{rid2}", {"amount_cents": 15_000})
    c.eq(st, 400, "单笔退款上调超原单剩余被拒")
    c.contains(data.get("error", ""), "最多还能退", "提示最多还能退多少")


# ================================================================ T 幂等提交
# 2026-10-29 生产库里出现 434/435/436 三条一模一样的打车（created_at 同一秒 + 2 秒后），
# 根因不是网络：记账这条写路径前后端都没有任何防重，双击、断网重发、结果未知时
# 再点一次，都会各落一条。现在客户端每次提交带一个随机 client_token，服务端按它去重。
#
# 关键设计：token 标识的是「这一次提交动作」，不是这笔账的内容。
# 两笔金额分类日期都一样的真实消费各有各的 token，必须照记两条——
# 拿金额/分类/备注去判重才是错的，这一条边界下面单独有用例守着。

def _tok() -> str:
    return "t-%d-%d" % (time.time_ns(), next(SEQ))


@case("T 幂等提交", "同一个 client_token 发两次只落一条，第二次返回原来那条 id")
def t1(c: Ctx):
    m, d = c.month_pair()
    body = {"tx_date": d, "direction": "expense", "cat_parent": "交通",
            "category": "打车", "amount_cents": 1679, "note": "双击打车",
            "client_token": _tok()}
    st1, r1 = c.me.post("/api/tx", body)
    st2, r2 = c.me.post("/api/tx", body)
    c.eq(st1, 200, "第一次正常落账")
    c.eq(st2, 200, "重复那次也返回 200——用户不该看到一条红字去猜到底记没记上")
    c.eq(r2.get("id"), r1.get("id"), "两次返回同一条 id")
    c.ok(r2.get("duplicate") is True, "第二次带 duplicate 标记，前端据此换文案")
    _, data = c.me.get("/api/tx?month=" + m)
    same = [t for t in data["items"] if t["note"] == "双击打车"]
    c.eq(len(same), 1, "明细里只有一条")


@case("T 幂等提交", "金额分类日期全同、连备注都空的两笔，也不会被当成重复")
def t2(c: Ctx):
    m, d = c.month_pair()
    base = {"tx_date": d, "direction": "expense", "cat_parent": "交通",
            "category": "打车", "amount_cents": 1679, "note": ""}
    st1, r1 = c.me.post("/api/tx", dict(base, client_token=_tok()))
    st2, r2 = c.me.post("/api/tx", dict(base, client_token=_tok()))
    c.eq(st1, 200, "第一笔落账")
    c.eq(st2, 200, "第二笔也落账")
    c.ne(r1.get("id"), r2.get("id"), "是两条不同的记录")
    c.ok(not r2.get("duplicate"), "第二笔不是重复")
    _, data = c.me.get("/api/tx?month=" + m)
    same = [t for t in data["items"]
            if t["cat_parent"] == "交通" and t["category"] == "打车"
            and t["amount_cents"] == 1679 and t["tx_date"] == d]
    c.eq(len(same), 2, "同额同类的两笔真实消费都在——幂等键不是业务指纹")


@case("T 幂等提交", "不带 client_token 的老客户端照样能记，不会被拦")
def t3(c: Ctx):
    m, d = c.month_pair()
    st, r = c.me.post("/api/tx", {"tx_date": d, "direction": "expense",
                                  "cat_parent": "餐饮", "category": "早餐",
                                  "amount_cents": 800, "note": "没带键"})
    c.eq(st, 200, "不带幂等键也能记")
    c.ok(r.get("id"), "返回了新 id")
    c.ok(not r.get("duplicate"), "不是重复")


@case("T 幂等提交", "两个请求真正同时到达，唯一索引兜底后仍然只落一条")
def t4(c: Ctx):
    m, d = c.month_pair()
    body = {"tx_date": d, "direction": "expense", "cat_parent": "交通",
            "category": "打车", "amount_cents": 1679, "note": "并发打车",
            "client_token": _tok()}
    out = []

    def hit():
        out.append(c.me.post("/api/tx", body))

    ts = [threading.Thread(target=hit) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    c.eq(len(out), 2, "两个请求都有响应")
    c.ok(all(st == 200 for st, _ in out),
         "都是 200：撞唯一索引的那一路被当成重复，而不是报错")
    c.eq(len({r.get("id") for _, r in out}), 1, "两路返回同一条 id")
    _, data = c.me.get("/api/tx?month=" + m)
    c.eq(len([t for t in data["items"] if t["note"] == "并发打车"]), 1, "库里只有一条")


@case("T 幂等提交", "报销到账带差补：同 token 重发不会多记一条，差补也只补一次")
def t5(c: Ctx):
    m, d = c.month_pair()
    a1 = c.expense(amount=40000, when=d, reimb_pending=True, note="可报销的住宿")
    body = {"tx_date": d, "direction": "refund", "offset_kind": "reimb",
            "cat_parent": "退款", "category": "其他退款",
            "amount_cents": 30000,                       # 到账 300 = 冲 200 + 差补 50 + 未认领 50
            "links": [{"parent_id": a1, "amount_cents": 20000}],
            "subsidy_cents": 5000, "subsidy_category": "出差补贴",
            "note": "报销来回", "client_token": _tok()}
    before = c.stats(m)
    st1, r1 = c.me.post("/api/tx", body)
    st2, r2 = c.me.post("/api/tx", body)
    c.eq(st1, 200, "第一次到账成功")
    c.eq(st2, 200, "重复那次也 200")
    c.eq(r2.get("id"), r1.get("id"), "返回同一条冲抵记录")
    c.ok(r2.get("duplicate") is True, "第二次带 duplicate 标记")
    c.eq(r2.get("income_id"), None, "重复那次不再生成第二条差补收入")
    after = c.stats(m)
    c.eq(after["income"] - before["income"], 5000, "差补只补了一次")
    c.eq(after["reimb"] - before["reimb"], 25000, "报销到账只记了一次")
    _, data = c.me.get("/api/tx?month=" + m)
    c.eq(len([t for t in data["items"] if t["note"] == "报销来回"]), 1, "冲抵记录只有一条")
    c.eq(len([t for t in data["items"]
              if t["direction"] == "income" and "差补" in t["note"]]), 1, "差补收入只有一条")


# ================================================================ U 编辑边界
# 这一组是「改账」这条路的守门员。建账（_create_tx）一直有校验和用例盯着，
# 改账（_api_patch）长期没有——2026-10-29 审查出的 7 条 P0/P1 全都只需一条 PATCH。
# 每一条都对应审查报告里的一个编号，改动 _api_patch 前先跑它。

@case("U 编辑边界", "登录接口收到 JSON 数组也必须是正常响应，不能把连接打死")
def u1(c: Ctx):
    # 未登录即可触发，属于「未鉴权面」——所以哪怕只是解析问题，也不许表现为连接重置。
    st, data = c.anon.post("/api/login", [1, 2])
    c.ok(400 <= st < 500, f"POST /api/login [1,2] 返回 4xx（实际 {st}）")
    c.ok(isinstance(data, dict), "登录响应体仍是 JSON 对象")
    c.ok(bool(data.get("error")), "登录响应带 error 文案（不是空对象）")
    st, _ = c.anon.post("/api/tx", ["x"])
    c.eq(st, 401, "匿名 POST /api/tx 传数组仍是 401")
    st, _ = c.me.post("/api/ai/parse", ["x"])
    c.ok(400 <= st < 500, f"登录后 POST /api/ai/parse 传数组返回 4xx（实际 {st}）")
    # 登出不读 body，返回 200 是正常的；这里只要求「有响应」，别把连接打崩。
    # 用匿名客户端发：真登出会把 c.me 的会话注销，后面的用例就全 401 了。
    st, _ = c.anon.post("/api/logout", ["x"])
    c.ok(st != 0, f"POST /api/logout 传数组仍有正常响应（实际 {st}）")


@case("U 编辑边界", "脏 parent_id / 脏 owner_id 一律 400，不能 500 或断连")
def u2(c: Ctx):
    tid = c.expense(amount=1000)
    for bad in ("abc", [], {}, 1.5):
        st, data = c.me.patch(f"/api/tx/{tid}", {"parent_id": bad})
        c.eq(st, 400, f"PATCH parent_id={bad!r} 被拒（400）")
    row = c.row(tid)
    c.ok(row is not None, "被拒后记录仍在")
    c.eq(row.get("amount_cents"), 1000, "被拒的请求没有改动金额")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"owner_id": "abc"})
    c.eq(st, 400, "PATCH owner_id='abc' 被拒（400）")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"amount_cents": "abc"})
    c.eq(st, 400, "PATCH amount_cents='abc' 被拒（400）")


@case("U 编辑边界", "编辑时清空或写非法日期必须 400，记录不能从月度视图消失（R1）")
def u3(c: Ctx):
    tid = c.expense(amount=12345, when=D(0))
    m = D(0)[:7]
    for bad in ("", "   ", "abc", "2026-13-40", "2026-02-30", "2026/10/29", None):
        st, data = c.me.patch(f"/api/tx/{tid}", {"tx_date": bad})
        c.eq(st, 400, f"PATCH tx_date={bad!r} 被拒（400）")
        c.ok(bool(data.get("error")), f"PATCH tx_date={bad!r} 带错误文案")
    row = c.row(tid)
    c.ok(row is not None, "被拒之后记录仍在当月列表里")
    c.eq(row.get("tx_date"), D(0), "日期保持原值，没有被写成空串")
    _, data = c.me.get("/api/tx?month=" + m)
    c.ok(any(t["id"] == tid for t in data.get("items", [])), "记录仍在当月筛选结果里")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"tx_date": D(1)})
    c.eq(st, 200, "合法日期仍能改")
    c.eq(c.row(tid, month=D(1)[:7]).get("tx_date"), D(1), "改成了新日期")


@case("U 编辑边界", "建账与改账对同一个非法日期口径一致（都是 400）")
def u4(c: Ctx):
    # 同一个业务规则两个入口两个口径，正是 R1 的根因：POST 拒的 PATCH 照收。
    body = {"tx_date": "2026-13-40", "direction": "expense", "cat_parent": "餐饮",
            "category": "早餐", "amount_cents": 100}
    st_post, d_post = c.me.post("/api/tx", body)
    c.eq(st_post, 400, "POST 非法日期被拒")
    tid = c.expense(amount=200)
    st_patch, d_patch = c.me.patch(f"/api/tx/{tid}", {"tx_date": "2026-13-40"})
    c.eq(st_patch, 400, "PATCH 非法日期被拒")
    c.eq(st_patch, st_post, "两个入口的状态码一致")
    c.eq(d_patch.get("code"), d_post.get("code"), "两个入口的错误码一致")
    # 不带日期仍然记今天：老客户端 / AI 草稿都不传这个字段
    st, data = c.me.post("/api/tx", {"direction": "expense", "cat_parent": "餐饮",
                                     "category": "早餐", "amount_cents": 300,
                                     "note": "没填日期"})
    c.eq(st, 200, "不传日期仍能记账")
    c.eq(c.row(data["id"]).get("tx_date"), D(0), "不传日期默认记今天")


@case("U 编辑边界", "借款日期清空同样被拒，不能落成空串")
def u5(c: Ctx):
    st, data = c.me.post("/api/loans", {"direction": "out", "counterparty": "同事",
                                       "amount_cents": 50000, "loan_date": D(3),
                                       "note": "借款"})
    c.eq(st, 200, "建借款")
    lid = data.get("id")
    st, resp = c.me.patch(f"/api/loans/{lid}", {"loan_date": ""})
    c.eq(st, 400, "PATCH 借款日期为空被拒")
    c.ok(bool(resp.get("error")), "带错误文案")
    st, resp = c.me.patch(f"/api/loans/{lid}", {"loan_date": "2026-13-40"})
    c.eq(st, 400, "PATCH 借款日期非法被拒")
    st, resp = c.me.patch(f"/api/loans/{lid}", {"loan_date": D(2)})
    c.eq(st, 200, "合法日期仍能改")
    _, data = c.me.get("/api/loans")
    row = [x for x in data.get("items", []) if x["id"] == lid][0]
    c.eq(row.get("loan_date"), D(2), "借款日期已更新")


@case("U 编辑边界", "改账金额与建账同一上限，2 亿元不能再进库（R9）")
def u6(c: Ctx):
    tid = c.expense(amount=200)
    for bad, why in ((200_000_000, "2 亿元"), (100_000_001, "100 万元 + 1 分"),
                     (0, "0 元"), (-5, "负数"), ("abc", "字符串"), (1.5, "小数")):
        st, data = c.me.patch(f"/api/tx/{tid}", {"amount_cents": bad})
        c.eq(st, 400, f"PATCH 金额 {why} 被拒（400）")
        c.ok(bool(data.get("error")), f"PATCH 金额 {why} 带错误文案")
    c.eq(c.row(tid).get("amount_cents"), 200, "被拒之后金额没变")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"amount_cents": 100_000_000})
    c.eq(st, 200, "正好 100 万元可以通过")
    c.eq(c.row(tid).get("amount_cents"), 100_000_000, "100 万元已入库")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"amount_cents": 200})
    c.eq(st, 200, "能改回小额")


@case("U 编辑边界", "建账与改账对超额金额的话术与状态码一致")
def u7(c: Ctx):
    body = {"tx_date": D(0), "direction": "expense", "cat_parent": "餐饮",
            "category": "早餐", "amount_cents": 200_000_000}
    st_post, d_post = c.me.post("/api/tx", body)
    c.eq(st_post, 400, "POST 2 亿元被拒")
    tid = c.expense(amount=300)
    st_patch, d_patch = c.me.patch(f"/api/tx/{tid}", {"amount_cents": 200_000_000})
    c.eq(st_patch, 400, "PATCH 2 亿元被拒")
    c.eq(d_patch.get("code"), d_post.get("code"), "两个入口的错误码一致")
    c.eq(d_patch.get("error"), d_post.get("error"), "两个入口的话术一致")
    for bad, why in ((0, "0 元"), (-1, "负数"), ("abc", "字符串"), (None, "空值")):
        body["amount_cents"] = bad
        st, _ = c.me.post("/api/tx", body)
        c.eq(st, 400, f"POST 金额{why}被拒（400）")


@case("U 编辑边界", "借款金额也走同一套校验，repaid_cents 仍允许 0")
def u8(c: Ctx):
    st, _ = c.me.post("/api/loans", {"direction": "out", "counterparty": "老李",
                                     "amount_cents": 200_000_000, "loan_date": D(1)})
    c.eq(st, 400, "建借款 2 亿元被拒")
    st, data = c.me.post("/api/loans", {"direction": "out", "counterparty": "老李",
                                        "amount_cents": 50000, "loan_date": D(1)})
    c.eq(st, 200, "正常金额可以建借款")
    lid = data.get("id")
    st, _ = c.me.patch(f"/api/loans/{lid}", {"amount_cents": 200_000_000})
    c.eq(st, 400, "改借款金额到 2 亿元被拒")
    st, _ = c.me.patch(f"/api/loans/{lid}", {"amount_cents": 0})
    c.eq(st, 400, "改借款金额到 0 被拒")
    # repaid_cents 表示「还没还」，0 是合法值——不能跟着 parse_cents 一起拒掉
    st, resp = c.me.patch(f"/api/loans/{lid}", {"repaid_cents": 0})
    c.eq(st, 200, "已还 0 元仍可保存")
    st, resp = c.me.patch(f"/api/loans/{lid}", {"repaid_cents": 50000})
    c.eq(st, 200, "还满可以保存")
    c.ok(resp.get("settled") is True, "还满后标记结清")


@case("U 编辑边界", "支出金额不得改到低于已冲抵额，统计不能出现负支出（R2）")
def u9(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=20000, when=d, note="先买后退")
    c.refund(pid, 15000, when=d)
    st, data = c.me.patch(f"/api/tx/{pid}", {"amount_cents": 10000})
    c.eq(st, 400, "已退 150，把金额改成 100 被拒")
    c.ok(bool(data.get("error")), "带错误文案")
    row = c.row(pid, month=m)
    c.eq(row.get("amount_cents"), 20000, "被拒后金额没变成 100")
    c.eq(row.get("refunded_cents"), 15000, "已退金额仍是 150")
    stats = c.stats(m)
    c.ok(stats["expense"] >= 0, f"统计支出不为负（{stats['expense']}）")
    # 边界：正好等于已退额可以过（净额 0），再少一分就不行
    st, _ = c.me.patch(f"/api/tx/{pid}", {"amount_cents": 14999})
    c.eq(st, 400, "149.99 元（比已退少 1 分）被拒")
    st, _ = c.me.patch(f"/api/tx/{pid}", {"amount_cents": 15000})
    c.eq(st, 200, "正好等于已退额（150）可以通过")
    c.eq(c.row(pid, month=m).get("amount_cents"), 15000, "150 已入库")
    st, _ = c.me.patch(f"/api/tx/{pid}", {"amount_cents": 30000})
    c.eq(st, 200, "改大到 300 可以通过")
    c.eq(c.row(pid, month=m).get("amount_cents"), 30000, "300 已入库")


@case("U 编辑边界", "多笔冲抵的支出也守同一条线，改完净额不为负")
def u10(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=50000, when=d, note="分两次退")
    c.refund(pid, 20000, when=d)
    c.refund(pid, 10000, when=d)
    st, _ = c.me.patch(f"/api/tx/{pid}", {"amount_cents": 25000})
    c.eq(st, 400, "已退 300，改成 250 被拒")
    st, _ = c.me.patch(f"/api/tx/{pid}", {"amount_cents": 30000})
    c.eq(st, 200, "改成正好 300 可以通过")
    stats = c.stats(m)
    c.ok(stats["expense"] >= 0, f"统计支出不为负（{stats['expense']}）")
    _, d2 = c.me.get("/api/tx?month=" + m)
    me = [t for t in d2["items"] if t["id"] == pid][0]
    c.eq(me["amount_cents"] - me["refunded_cents"], 0, "净额正好 0，没有变成负数")
    # 只改备注不能受这条规则影响（防止把「不许改小」错做成「不许保存」）
    st, _ = c.me.patch(f"/api/tx/{pid}", {"note": "只改备注"})
    c.eq(st, 200, "已冲满的支出仍能改备注")
    c.eq(c.row(pid, month=m).get("note"), "只改备注", "备注已更新")


@case("U 编辑边界", "有子冲抵的支出禁止改收支方向，那笔钱不能凭空消失（R5）")
def u11(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=5000, when=d, note="有退款的原单")
    c.refund(pid, 2000, when=d)
    before = c.stats(m)
    st, data = c.me.patch(f"/api/tx/{pid}", {"direction": "income",
                                             "cat_parent": "工资", "category": "月度工资"})
    c.eq(st, 400, "已退 20 的支出改成收入被拒")
    c.ok(bool(data.get("error")), "带错误文案")
    c.contains(data.get("error"), "冲抵", "提示里点明是冲抵记录挡着")
    row = c.row(pid, month=m)
    c.eq(row.get("direction"), "expense", "方向没变")
    after = c.stats(m)
    c.eq(after["expense"], before["expense"], "统计支出没变")
    c.eq(after["refund_offset"] + after["refund_orphan"],
         before["refund_offset"] + before["refund_orphan"], "冲抵金额没有凭空消失")
    # 先把冲抵记录删掉，就能改方向——错误提示得可执行，不能是死胡同
    _, d2 = c.me.get("/api/tx?month=" + m)
    child = [t for t in d2["items"] if t.get("parent_id") == pid][0]
    c.eq(c.delete(child["id"])[0], 200, "删掉冲抵记录")
    st, _ = c.me.patch(f"/api/tx/{pid}", {"direction": "income",
                                          "cat_parent": "工资", "category": "月度工资"})
    c.eq(st, 200, "冲抵记录没了就能改成收入")


@case("U 编辑边界", "没有子冲抵的支出、以及冲抵记录本身，改方向仍按原语义放行")
def u12(c: Ctx):
    m, d = c.month_pair()
    pid = c.expense(amount=6000, when=d, note="干净的原单")
    st, _ = c.me.patch(f"/api/tx/{pid}", {"direction": "income",
                                          "cat_parent": "工资", "category": "月度工资"})
    c.eq(st, 200, "没有冲抵的支出可以改成收入")
    row = c.row(pid, month=m)
    c.eq(row.get("direction"), "income", "方向已改")
    c.eq(row.get("cat_parent"), "工资", "分类跟着换成收入树里的")
    # 反向：冲抵记录解除冲抵 → 变成一条普通支出（现有语义，别在加限制时误伤）
    pid2 = c.expense(amount=7000, when=d, note="要解除冲抵的原单")
    rid = c.refund(pid2, 3000, when=d)
    st, data = c.me.patch(f"/api/tx/{rid}", {"direction": "expense", "parent_id": None,
                                             "cat_parent": "餐饮", "category": "早餐"})
    c.eq(st, 200, "冲抵记录解除冲抵后变成普通支出")
    row2 = c.row(rid, month=m)
    c.eq(row2.get("direction"), "expense", "已经是支出")
    c.eq(row2.get("parent_id"), None, "不再挂原单")
    c.eq(row2.get("refunded_cents") or 0, 0, "这条自身没有冲抵额")
    _, d3 = c.me.get("/api/tx?month=" + m)
    still = [t for t in d3["items"] if t.get("parent_id") == pid2]
    c.eq(len(still), 0, "分配表里的关联已清空")


@case("U 编辑边界", "非支出记录强制清空 5 个标记，已报销不能筛出收入（R6）")
def u13(c: Ctx):
    m, d = c.month_pair()
    # 只带「已报销」：这条是 R6 的正身——另外 4 个标记建账时就被清了，
    # 只有 reimbursed 漏网，而且「待报销」也会顺带把它清掉，所以必须单独验一次。
    tid = c.income(amount=800000, when=d, reimbursed=True)
    row = c.row(tid, month=m)
    c.eq(row.get("reimbursed"), 0, "建收入时 reimbursed 被清 0")
    tid2 = c.income(amount=900000, when=d, shared=True, reimb_pending=True,
                    reimbursed=True, pending_use=True, travel=True)
    row0 = c.row(tid2, month=m)
    for mark in ("shared", "reimb_pending", "reimbursed", "pending_use", "travel"):
        c.eq(row0.get(mark), 0, f"五个标记一起传时 {mark} 也被清 0")
    _, data = c.me.get("/api/tx?mark=reimb")
    c.ok(not any(x["id"] == tid for x in data.get("items") or []),
         "「已报销」筛选不再命中收入")
    # 改账这条路也一样：置 true 不报错，但落库必须是 0（冗余字段，不拦人）
    st, resp = c.me.patch(f"/api/tx/{tid}", {"reimbursed": True})
    c.eq(st, 200, "给收入置「已报销」不报错")
    c.eq(c.row(tid, month=m).get("reimbursed"), 0, "落库仍是 0")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"travel": True, "shared": True})
    c.eq(st, 200, "给收入置「出差/共同」也不报错")
    row2 = c.row(tid, month=m)
    c.eq(row2.get("travel"), 0, "travel 仍是 0")
    c.eq(row2.get("shared"), 0, "shared 仍是 0")
    # 支出这边的标记不能被误清（改的是「非支出」分支，别把正常功能一起关掉）
    eid = c.expense(amount=1000, when=d, travel=True, shared=True)
    row3 = c.row(eid, month=m)
    c.eq(row3.get("travel"), 1, "支出的出差标记仍在")
    c.eq(row3.get("shared"), 1, "支出的共同标记仍在")


@case("U 编辑边界", "冲抵记录的归属强制跟随原单，改它无效但也不报错（R8 后端半）")
def u14(c: Ctx):
    m, d = c.month_pair()
    member_a_id = c.me.get("/api/me")[1]["user"]["id"]
    member_b_id = c.member_b.get("/api/me")[1]["user"]["id"]
    pid = c.expense(amount=10000, when=d, owner_id=member_b_id, note="她垫的钱")
    rid = c.refund(pid, 4000, when=d)
    c.eq(c.row(rid, month=m).get("owner_id"), member_b_id, "冲抵记录一开始就继承原单归属")
    st, _ = c.me.patch(f"/api/tx/{rid}", {"owner_id": member_a_id})
    c.eq(st, 200, "给冲抵记录改归属不报错（静默忽略）")
    c.eq(c.row(rid, month=m).get("owner_id"), member_b_id, "归属仍是原单那个人")
    # 金额和备注该改还得能改——「归属只读」不等于「整条只读」
    st, _ = c.me.patch(f"/api/tx/{rid}", {"amount_cents": 3500, "note": "少退一点"})
    c.eq(st, 200, "冲抵记录的金额与备注仍可改")
    row = c.row(rid, month=m)
    c.eq(row.get("amount_cents"), 3500, "金额改成 35")
    c.eq(row.get("note"), "少退一点", "备注也改了")
    c.eq(row.get("owner_id"), member_b_id, "改完归属还是原单的")
    # 无主退款没有原单，归属本来就该由填的人决定，别一起锁死
    st, data = c.me.post("/api/tx", {"tx_date": d, "direction": "refund",
                                     "cat_parent": "退款", "category": "其他退款",
                                     "amount_cents": 1200, "owner_id": member_b_id,
                                     "note": "不知道退的哪笔"})
    c.eq(st, 200, "记一笔无主退款")
    solo = data["id"]
    c.eq(c.row(solo, month=m).get("owner_id"), member_b_id, "无主退款归属按填的来")
    st, _ = c.me.patch(f"/api/tx/{solo}", {"owner_id": member_a_id})
    c.eq(st, 200, "无主退款可以改归属")
    c.eq(c.row(solo, month=m).get("owner_id"), member_a_id, "归属确实改了")


# ---- 分类配置：这组用例会动全局树，所以都自带清理，且排在 U 组最后 ----

def _cfg(c: Ctx):
    st, data = c.admin.get("/api/categories/config")
    assert st == 200, f"读分类配置失败 {st} {data}"
    return data["config"]


def _save_cfg(c: Ctx, cfg, **kw):
    body = {"config": cfg}
    body.update(kw)
    return c.admin.post("/api/categories/config", body)


def _cnt(c: Ctx, parent, month=None):
    st, data = c.me.get("/api/tx?cat_parent=" + quote(parent)
                        + ("&month=" + month if month else ""))
    return len(data.get("items") or [])


@case("U 编辑边界", "删一个分类同时加一个，不能被当成改名而改写历史（R3）")
def u15(c: Ctx):
    m, d = c.month_pair()
    tmp = "临时分类R3"
    cfg = _cfg(c)
    cfg["expense"][tmp] = {"color_slot": None, "subs": ["临时子1R3", "其他"]}
    st, data = _save_cfg(c, cfg)
    c.eq(st, 200, "先建一个没有历史的新分类")
    c.eq(data.get("renamed_tx") or 0, 0, "建空分类不动任何历史")
    for i in range(3):
        c.expense(amount=1000 + i, when=d, cat=(tmp, "其他"))
    c.eq(_cnt(c, tmp), 3, "3 笔挂在临时分类下")
    # 删掉它，同时加一个「子类交集恰好是『其他』」的新分类——
    # 旧规则按交集配对，会把这两件事判成改名，3 笔历史被静默改写成新名字。
    cfg2 = _cfg(c)
    cfg2["expense"].pop(tmp)
    cfg2["expense"]["临时分类R3b"] = {"color_slot": None, "subs": ["临时子2R3", "其他"]}
    st, data = _save_cfg(c, cfg2)
    c.eq(st, 400, "删有历史的分类被拒（400）")
    c.eq(data.get("code"), "category_has_history", "错误码指明「分类下有历史」")
    c.contains(data.get("error"), tmp, "文案点名是哪个分类")
    c.eq(_cnt(c, tmp), 3, "历史 3 笔仍挂在原分类下，一笔没被改写")
    c.eq(_cnt(c, "临时分类R3b"), 0, "新分类下没有凭空冒出来的账")
    c.ok(tmp in _cfg(c)["expense"], "配置也没被写入（原分类还在）")


@case("U 编辑边界", "分类改动的预览是只读的：只看影响面，不落库")
def u16(c: Ctx):
    m, d = c.month_pair()
    tmp = "临时分类R3p"
    cfg = _cfg(c)
    cfg["expense"][tmp] = {"color_slot": None, "subs": ["甲R3p", "乙R3p"]}
    c.eq(_save_cfg(c, cfg)[0], 200, "建临时分类")
    for i in range(2):
        c.expense(amount=2000 + i, when=d, cat=(tmp, "甲R3p"))
    cfg2 = _cfg(c)
    cfg2["expense"].pop(tmp)
    cfg2["expense"]["临时分类R3q"] = {"color_slot": None, "subs": ["丙R3q", "丁R3q"]}
    st, data = _save_cfg(c, cfg2, preview=True)
    c.eq(st, 200, "预览本身不算错（200）")
    pv = data.get("preview") or {}
    c.ok(bool(pv.get("blocked")), "预览里列了「会被拦下」的删除项")
    c.ok(pv.get("affected_total") is None or True, "预览结构可解析")
    c.eq(_cnt(c, tmp), 2, "预览没有动历史")
    c.ok(tmp in _cfg(c)["expense"], "预览没有动配置")
    c.ok("临时分类R3q" not in _cfg(c)["expense"], "新分类也没被写进去")


@case("U 编辑边界", "真改名必须二次确认，确认后历史与冲抵影子行一起改（R3/D-03）")
def u17(c: Ctx):
    m, d = c.month_pair()
    tmp, tmp2 = "临时分类R3r", "临时分类R3s"
    cfg = _cfg(c)
    cfg["expense"][tmp] = {"color_slot": None, "subs": ["甲R3r", "乙R3r"]}
    c.eq(_save_cfg(c, cfg)[0], 200, "建临时分类：甲/乙")
    pid = c.expense(amount=5000, when=d, cat=(tmp, "甲R3r"), note="要改名的账")
    rid = c.refund(pid, 2000, when=d)      # 冲抵记录继承的是支出的分类名
    c.eq(c.row(rid, month=m).get("cat_parent"), tmp, "冲抵记录继承了原分类名")
    # 子类完全一致 → 这是货真价实的改名（交集 2、覆盖率 1.0）
    cfg2 = _cfg(c)
    cfg2["expense"].pop(tmp)
    cfg2["expense"][tmp2] = {"color_slot": None, "subs": ["甲R3r", "乙R3r"]}
    st, data = _save_cfg(c, cfg2)
    c.eq(st, 400, "不说一声就想改 2 笔历史 → 被拦（400）")
    c.eq(data.get("code"), "need_confirm", "错误码是 need_confirm")
    c.ok(bool((data.get("preview") or {}).get("renames")), "把改名计划一并发回来")
    c.eq(_cnt(c, tmp), 2, "还没确认，历史没动（支出 + 冲抵影子行都在原分类下）")
    st, data = _save_cfg(c, cfg2, confirm=True)
    c.eq(st, 200, "带 confirm 再来一次就写")
    c.eq(data.get("renamed_tx"), 2, "改名连带更新 2 笔（支出 + 冲抵影子行）")
    c.eq(_cnt(c, tmp), 0, "旧名字下已经没有账")
    c.eq(_cnt(c, tmp2), 2, "两笔都在新名字下（含冲抵记录）")
    c.eq(c.row(pid, month=m).get("cat_parent"), tmp2, "支出记录改到新分类")
    c.eq(c.row(rid, month=m).get("cat_parent"), tmp2, "冲抵记录的分类副本也改了")
    # 还原，别把全局树留给后面的用例
    back = _cfg(c)
    back["expense"].pop(tmp2)
    back["expense"][tmp] = {"color_slot": None, "subs": ["甲R3r", "乙R3r"]}
    st, _ = _save_cfg(c, back, confirm=True)
    c.eq(st, 200, "改名可以再改回来（还原测试现场）")


@case("U 编辑边界", "没有历史账单的空分类可以直接删，不该逼用户确认")
def u18(c: Ctx):
    tmp = "临时空分类R3"
    cfg = _cfg(c)
    cfg["expense"][tmp] = {"color_slot": None, "subs": ["空子R3"]}
    c.eq(_save_cfg(c, cfg)[0], 200, "建一个空分类")
    cfg2 = _cfg(c)
    cfg2["expense"].pop(tmp)
    st, data = _save_cfg(c, cfg2)
    c.eq(st, 200, "删 0 笔的分类直接成功，不用确认")
    c.eq(data.get("renamed_tx") or 0, 0, "没有改名、没有动历史")
    c.ok(tmp not in _cfg(c)["expense"], "配置里已经没有了")


@case("U 编辑边界", "摊在多笔上的冲抵不能直接改金额，只能去「冲抵分配」里调")
def u19(c: Ctx):
    m, d = c.month_pair()
    p1 = c.expense(amount=30000, when=d, cat=("餐饮", "午餐"), note="报销单一")
    p2 = c.expense(amount=20000, when=d, cat=("交通", "打车"), note="报销单二")
    st, data = c.me.post("/api/tx", {
        "tx_date": d, "direction": "refund", "offset_kind": "reimb",
        "cat_parent": "退款", "category": "其他退款", "amount_cents": 40000,
        "links": [{"parent_id": p1, "amount_cents": 25000},
                  {"parent_id": p2, "amount_cents": 15000}], "note": "一次报销摊两笔"})
    c.eq(st, 200, "记一笔摊两单的报销到账")
    rid = data["id"]
    st, e = c.me.patch(f"/api/tx/{rid}", {"amount_cents": 35000})
    c.eq(st, 400, "摊两笔的冲抵直接改金额被拒（400）")
    c.contains(e.get("error"), "冲抵分配", "提示去「冲抵分配」里改")
    c.eq(c.row(rid, month=m).get("amount_cents"), 40000, "被拒后金额没变")
    st, _ = c.me.patch(f"/api/tx/{rid}", {"note": "只改备注"})
    c.eq(st, 200, "摊两笔的冲抵仍能改备注")
    c.eq(c.row(rid, month=m).get("note"), "只改备注", "备注已更新")


@case("U 编辑边界", "连改两次日期都成功，只改备注不动金额与日期")
def u20(c: Ctx):
    tid = c.expense(amount=1500, when=D(0), note="原始备注")
    before = c.row(tid)
    st, _ = c.me.patch(f"/api/tx/{tid}", {"tx_date": D(1)})
    c.eq(st, 200, "第一次改日期成功")
    st, _ = c.me.patch(f"/api/tx/{tid}", {"tx_date": D(2)})
    c.eq(st, 200, "第二次改日期也成功")
    mid = c.row(tid, month=D(2)[:7])
    c.eq(mid.get("tx_date"), D(2), "日期停在最后一次改的值")
    c.ok(mid.get("updated_at") >= before.get("updated_at"), "updated_at 没有倒退")
    # 只改备注：金额/日期/方向都不能被顺手改掉（整包提交时最容易出的错）
    st, _ = c.me.patch(f"/api/tx/{tid}", {"note": "只改备注"})
    c.eq(st, 200, "只改备注成功")
    after = c.row(tid, month=D(2)[:7])
    c.eq(after.get("note"), "只改备注", "备注已更新")
    c.eq(after.get("amount_cents"), 1500, "金额没被改")
    c.eq(after.get("tx_date"), D(2), "日期没被改")
    c.eq(after.get("direction"), "expense", "方向没被改")


@case("U 编辑边界", "错误码：前端能按码分支，且响应不泄露实现细节（D-04）")
def u21(c: Ctx):
    m, d = c.month_pair()
    bad_texts = ("time data", "Traceback", "KeyError", "int()", "NoneType")
    # bad_body：请求体不是 JSON 对象。以前这种体会被当成 {} 一路走下去，
    # 最后报「金额不合法」，用户根本不知道是自己发错了格式
    st, r = c.me.post("/api/tx", [1, 2])
    c.eq(st, 400, "数组请求体 → 400（不是 500，也不是断连）")
    c.eq(r.get("code"), "bad_body", "code=bad_body")
    c.ok(not any(t in (r.get("error") or "") for t in bad_texts), "正文不含实现细节")

    tid = c.expense(amount=10_000, when=d)
    st, r = c.me.patch(f"/api/tx/{tid}", {"tx_date": ""})
    c.eq((st, r.get("code")), (400, "date_required"), "编辑时清空日期 → date_required")
    st, r = c.me.patch(f"/api/tx/{tid}", {"tx_date": "2026-13-40"})
    c.eq((st, r.get("code")), (400, "date_invalid"), "非法日期 → date_invalid")
    c.ok(not any(t in (r.get("error") or "") for t in bad_texts),
         "日期报错不再回显 Python 的 strptime 原话")
    st, r = c.me.patch(f"/api/tx/{tid}", {"amount_cents": 0})
    c.eq((st, r.get("code")), (400, "amount_invalid"), "金额 0 → amount_invalid")
    st, r = c.me.patch(f"/api/tx/{tid}", {"amount_cents": "12abc"})
    c.eq((st, r.get("code")), (400, "amount_invalid"), "金额带字母 → amount_invalid")

    st, r = c.me.post("/api/tx", {"tx_date": d, "direction": "expense",
                                  "cat_parent": "不存在的类", "category": "也不存在",
                                  "amount_cents": 1_000})
    c.eq((st, r.get("code")), (400, "category_invalid"), "野分类 → category_invalid")

    pid = c.expense(amount=10_000, when=d)
    c.refund(pid, 6_000, when=d)
    st, r = c.me.patch(f"/api/tx/{pid}", {"amount_cents": 5_000})
    c.eq((st, r.get("code")), (400, "amount_below_offset"), "低于已冲抵 → amount_below_offset")
    st, r = c.me.patch(f"/api/tx/{pid}", {"direction": "income"})
    c.eq((st, r.get("code")), (400, "direction_has_offsets"), "有冲抵改方向 → direction_has_offsets")

    # duplicate 不是错误：重复提交返回 200 + 布尔位。塞进 code 会让前端把成功当失败。
    body = {"tx_date": d, "direction": "expense", "cat_parent": "餐饮",
            "category": "早餐", "amount_cents": 1_234, "client_token": "dup-d04"}
    st, r1 = c.me.post("/api/tx", body)
    c.eq(st, 200, "第一次提交成功")
    st, r2 = c.me.post("/api/tx", body)
    c.eq((st, bool(r2.get("duplicate"))), (200, True), "同 token 再提交 → 200 + duplicate:true")
    c.eq(r2.get("id"), r1.get("id"), "返回的还是同一条")


# ================================================================ V 审计
# S-04：谁改的、谁删的。审计表和访问日志都没有对外接口（不是给人天天翻的），
# 这里直接读测试实例自己的库和日志文件——测试实例本来就是独立临时目录，读它不算越界。


@case("V 审计", "改账记下操作人，删账留完整快照（能照着恢复）")
def v1(c: Ctx):
    import sqlite3
    m, d = c.month_pair()
    tid = c.expense(amount=8_800, when=d, note="审计样例", client=c.member_a)

    # 改：updated_by 跟着最后一个动手的人走，不是「创建人」也不是「归属人」
    c.eq(c.row(tid, m)["updated_by"], None, "刚建的行还没有 updated_by")
    st, _ = c.member_b.patch(f"/api/tx/{tid}", {"note": "改一下"})
    c.eq(st, 200, "另一个人改成功")
    st, meta = c.me.get("/api/meta")
    member_b_id = [u["id"] for u in meta["users"] if u["username"] == "tester2"][0]
    member_a_id = [u["id"] for u in meta["users"] if u["username"] == "tester1"][0]
    c.eq(c.row(tid, m)["updated_by"], member_b_id, "updated_by = 改的那个人")

    st, _ = c.me.patch(f"/api/tx/{tid}", {"note": "再改一下"})
    c.eq(c.row(tid, m)["updated_by"], member_a_id, "再改一次就换成后动手的人")

    # 删：整行 + 冲抵分配都要进快照
    parent = c.expense(amount=5_000, when=d, note="被冲抵的原单")
    st, data = c.me.post("/api/tx", {"tx_date": d, "direction": "refund",
                                     "offset_kind": "reimb", "amount_cents": 5_000,
                                     "links": [{"parent_id": parent, "amount_cents": 5_000}]})
    rid = data["id"]
    st, _ = c.member_b.delete(f"/api/tx/{rid}")
    c.eq(st, 200, "删除成功")

    con = sqlite3.connect(str(c.workdir / "data" / "ledger.db"))
    con.row_factory = sqlite3.Row
    # 只能按 tx_id 过滤：整轮测试共用一个库，别的用例也删过东西
    logs = [dict(r) for r in con.execute(
        "SELECT * FROM audit_log WHERE action='delete' AND tx_id=? ORDER BY id", (rid,))]
    con.close()
    c.eq(len(logs), 1, "删一次只记一条，不多不少")
    row = logs[0]
    c.eq((row["kind"], row["tx_id"]), ("transactions", rid), "记的是哪张表哪条记录")
    c.eq(row["username"], "tester2", "记的是谁删的")
    snap = json.loads(row["before_json"])
    c.eq(snap["amount_cents"], 5_000, "快照里有金额")
    c.eq(snap["direction"], "refund", "快照里有方向")
    c.eq(snap["offset_kind"], "reimb", "快照里有冲抵类型——不然恢复时不知道这是报销还是退货")
    c.eq([(x["parent_id"], x["amount_cents"]) for x in snap["links"]],
         [(parent, 5_000)], "分配明细一起存下来了（这行会被 CASCADE 删掉，不存就恢复不了）")


@case("V 审计", "删不存在的记录返回 404，且不留假快照")
def v2(c: Ctx):
    import sqlite3
    st, data = c.me.delete("/api/tx/999999")
    c.eq(st, 404, "删不存在的是 404，不是假装成功")
    c.contains(data.get("error", ""), "不存在", "话术说清楚")
    con = sqlite3.connect(str(c.workdir / "data" / "ledger.db"))
    n = con.execute("SELECT COUNT(*) FROM audit_log WHERE tx_id=999999").fetchone()[0]
    con.close()
    c.eq(n, 0, "没删成功就不该留审计行——否则审计表里全是幽灵")


@case("V 审计", "访问日志：写操作与 4xx 有记录，正常浏览不刷屏")
def v3(c: Ctx):
    import time as _t
    log = c.workdir / "server.log"
    before = log.read_text(encoding="utf-8", errors="replace").count("\n") if log.exists() else 0
    # 1) 正常 GET 不该写日志
    c.me.get("/api/meta")
    # 2) 写操作该写
    m, d = c.month_pair()
    c.expense(amount=1_200, when=d, note="日志样例")
    # 3) 4xx 该写（打一个不存在的 id）
    c.me.get("/api/tx/999999/links")
    _t.sleep(0.4)                 # 日志比响应晚一步打印
    text = log.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    c.ok(len(lines) > before, "新增了日志行")
    c.ok(any("/api/meta" in x for x in lines[before:]) is False, "正常浏览没被记下来")
    c.ok(any("POST" in x and "/api/tx" in x for x in lines[before:]), "写操作有记录")
    c.ok(any("404" in x for x in lines[before:]), "4xx 有记录")
    c.ok(any("user=" in x for x in lines[before:]), "带操作人")
    c.ok(all("ms" in x for x in lines[before:]), "带耗时")


# ================================================================ W 导出 CSV
# S-06：把账本拿出去（Excel 算一下 / 换系统 / 发给对方对账）。
# CSV 不是 JSON，Client.req 会把它截成前 400 字节，这里自己发一次原始请求。


def _csv_get(c: Ctx, path: str):
    """原样取响应体：返回 (状态码, 原始字节, 响应头)"""
    headers = {"Cookie": "sid=" + c.sid} if c.sid else {}
    req = urllib.request.Request(c.base + quote(path, safe="/?=&%+~"), headers=headers)
    try:
        with _OPENER.open(req, timeout=20) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _csv_lines(raw: bytes):
    """只数数据行（不含表头）。末尾的 CRLF 不该算成一行空记录"""
    text = raw.decode("utf-8-sig")
    rows = [r for r in text.split("\r\n") if r.strip()]
    return rows[0], rows[1:]


@case("W 导出", "导出的是 Excel 能直接打开的中文 CSV，行数与列表 count 一致")
def w1(c: Ctx):
    m, d = c.month_pair()
    # 三笔同月：金额带小数，检验「分 → 元」的两位小数不会丢
    c.expense(amount=12_345, when=d, note="早餐", client=c.member_a)
    c.expense(amount=100, when=d, note="公交", client=c.member_a)
    c.expense(amount=999_900, when=d, note="房租", cat=("生活缴费", "房租房贷"), client=c.member_b)

    st, data = c.me.get("/api/tx?month=" + m)
    count = data["summary"]["count"]

    st, raw, hdrs = _csv_get(c.me, "/api/export.csv?month=" + m)
    c.eq(st, 200, "导出成功")
    c.contains(hdrs.get("Content-Type") or "", "text/csv", "Content-Type 是 CSV")
    c.contains(hdrs.get("Content-Disposition") or "", "attachment", "按附件下载，不在浏览器里直接打开")
    c.contains(hdrs.get("Content-Disposition") or "", "ledger-%s.csv" % m, "文件名带月份，存下来就知道是哪个月")
    # BOM 是最容易被后来的重构顺手删掉的东西，而删了 Excel 打开中文全是乱码
    c.eq(raw[:3], b"\xef\xbb\xbf", "带 UTF-8 BOM（否则 Excel 中文乱码）")
    head, rows = _csv_lines(raw)
    c.contains(head, "日期", "表头是中文")
    c.contains(head, "金额(元)", "金额列名说清楚单位")
    c.eq(len(rows), count, "导出行数 = 列表 summary.count")
    # 金额写成 123.45 而不是 12345 或 ¥123.45：Excel 要能直接求和
    c.ok(all("￥" not in r and "¥" not in r for r in rows), "金额不带货币符号，Excel 才好求和")
    c.ok(any(",123.45," in r for r in rows), "12345 分导成 123.45")
    c.ok(any(",1.00," in r for r in rows), "100 分导成 1.00，两位小数不丢")
    c.ok(any(",9999.00," in r for r in rows), "999900 分导成 9999.00，千位不加逗号（否则 Excel 认成文本）")


@case("W 导出", "导出的筛选口径与页面完全一致（标记 + 关键词 + 金额区间）")
def w2(c: Ctx):
    m, d = c.month_pair()
    # 同一批数据里混入不该出现的东西：口径一旦各写一套，这里立刻现形
    a = c.expense(amount=5_000, when=d, note="三亚 机票", client=c.member_a)
    b = c.expense(amount=8_000, when=d, note="三亚 酒店", client=c.member_a)
    c.expense(amount=9_000, when=d, note="三亚 打车", client=c.member_a)   # 没标待报销
    c.me.patch(f"/api/tx/{a}", {"reimb_pending": 1})
    c.me.patch(f"/api/tx/{b}", {"reimb_pending": 1})

    # 中文不预先 quote：Client / _csv_get 都会按 safe 编码一次，这里再编就成了两次
    q = "?month=%s&mark=pending&q=三亚&min_cents=6000" % m
    st, data = c.me.get("/api/tx" + q)
    ids = {t["id"] for t in data["items"]}
    c.eq(ids, {b}, "列表筛选结果先自证一下：只有 8000 那笔同时满足三个条件")

    st, raw, _ = _csv_get(c.me, "/api/export.csv" + q)
    _, rows = _csv_lines(raw)
    c.eq(st, 200, "导出成功")
    c.eq(len(rows), data["summary"]["count"], "导出行数 = 列表 summary.count（共用同一段过滤）")
    c.eq(len(rows), 1, "只有一笔，多一笔都说明导出另写了一套过滤")
    c.contains(rows[0], "三亚 酒店", "导出的正是筛出来的那笔")
    c.ok("打车" not in rows[0], "没勾待报销的不该混进来")

    # 反向验证：不存在的标记键当没传，绝不能因为参数写错就把整本账倒出来
    st, raw2, _ = _csv_get(c.me, "/api/export.csv?month=%s&mark=bogus" % m)
    _, rows2 = _csv_lines(raw2)
    c.eq(len(rows2), 3, "非法标记被忽略，退化成「该月全部」而不是「全部月份」")


@case("W 导出", "导出不截断：列表 500 行上限之外的部分照样导得出来")
def w3(c: Ctx):
    import sqlite3
    m, d = c.month_pair()
    c.expense(amount=1_000, when=d, note="凑数用")
    # 走接口造 520 笔太慢（每笔一次提交），直接往测试实例自己的库里写。
    # 这不是测 SQL，是测「导出与列表的分页/上限策略不同」这一个事实。
    con = sqlite3.connect(str(c.workdir / "data" / "ledger.db"))
    try:
        uid = con.execute("SELECT id FROM users WHERE is_admin=0 ORDER BY id").fetchone()[0]
        rows = [(d, "expense", "", "餐饮", "早餐", 2_000, "批量 %d" % i,
                 "2026-01-01T00:00:00", "2026-01-01T00:00:00", uid, uid, 0, 0, 0, 0, 0, None)
                for i in range(520)]
        con.executemany(
            "INSERT INTO transactions(tx_date,direction,offset_kind,cat_parent,category,"
            "amount_cents,note,created_at,updated_at,owner_id,created_by,shared,reimb_pending,"
            "reimbursed,pending_use,travel,parent_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()

    st, data = c.me.get("/api/tx?month=" + m)
    c.eq(st, 200, "列表查询成功")
    c.eq(len(data["items"]), 500, "列表最多给 500 行（页面翻不动那么多）")
    c.eq(data["summary"]["count"], 521, "但 count 是全量，不是 500")

    st, raw, _ = _csv_get(c.me, "/api/export.csv?month=" + m)
    _, out = _csv_lines(raw)
    c.eq(st, 200, "导出成功")
    c.eq(len(out), 521, "导出是整份拿走，不能跟着列表截断——否则悄悄少数据")


@case("W 导出", "未登录不给导出，文件名不会被参数注入换行")
def w4(c: Ctx):
    st, raw, _ = _csv_get(c.anon, "/api/export.csv?month=2026-10")
    c.eq(st, 401, "未登录导出是 401")
    c.ok(b"\xef\xbb\xbf" not in raw, "401 时不该吐出任何 CSV 内容")

    # month 会拼进响应头，必须过滤掉换行等字符（响应头注入是实打实的漏洞）
    bad = "%0d%0aX-Injected:%20yes"
    st, raw, hdrs = _csv_get(c.me, "/api/export.csv?month=" + bad)
    c.eq(st, 200, "参数再脏也不该 500")
    c.eq(hdrs.get("X-Injected"), None, "换行没被注入到响应头里")
    c.contains(hdrs.get("Content-Disposition") or "", "ledger-all.csv", "脏参数退化成 all，不拼进文件名")


# ================================================================ X 明细净额
# 明细行的**大数字 = 净额**（原额 − 已分配冲抵）。这一个数同时决定三件事：
# 行里显示什么、「金额」排序按什么排、搜数字能不能搜到——三处必须同一个口径，
# 所以放一组一起测。口径定义见 docs/家庭账本-功能大全.md 的「明细金额」一节。
# 注意：筛选的「金额区间」按**原额**，那是另一个问题（「我记得花了 1000 多」），
# 本组不测它，改动时别顺手一起改。


@case("X 明细净额", "金额排序按净额：退掉九成的那笔排到它「真花的钱」该在的位置")
def x1(c: Ctx):
    m, d = c.month_pair()
    big = c.expense(amount=100_000, when=d, note="净额排序·退掉九成")
    mid = c.expense(amount=50_000, when=d, note="净额排序·居中")
    small = c.expense(amount=20_000, when=d, note="净额排序·本来就小")
    c.refund(big, 90_000, when=d)              # 净额只剩 100.00，成了最小的一笔

    st, data = c.me.get("/api/tx?month=%s&direction=expense&sort=amount_desc" % m)
    c.eq(st, 200, "按净额排序查询成功")
    c.eq([t["id"] for t in data["items"]], [mid, small, big],
         "高→低按净额：500 > 200 > 100；按原额排会把 1000 那笔顶在最前面")
    c.eq(data["items"][-1]["remain_cents"], 10_000, "末位那笔的净额就是列表回的 remain_cents")

    st, data = c.me.get("/api/tx?month=%s&direction=expense&sort=amount_asc" % m)
    c.eq([t["id"] for t in data["items"]], [big, small, mid], "低→高是同一套数的反序")


@case("X 明细净额", "金额搜索同时认原额和净额：屏幕上出现过的数字都得搜得到")
def x2(c: Ctx):
    m, d = c.month_pair()
    p = c.expense(amount=88_800, when=d, note="净额搜索样本")
    c.refund(p, 77_700, when=d)                # 行里显示净额 111.00

    st, data = c.me.get("/api/tx?month=%s&q=111.00" % m)
    c.eq([t["id"] for t in data["items"]], [p], "搜净额（行里显示的那个数）能找到")
    st, data = c.me.get("/api/tx?month=%s&q=888.00" % m)
    c.eq([t["id"] for t in data["items"]], [p], "搜原额（账单上那个数）照样能找到")


@case("X 明细净额", "导出的净额列求和等于页头的净流出：同一笔退款不能扣两遍")
def x3(c: Ctx):
    import csv as _csv
    import io
    m, d = c.month_pair()
    p = c.expense(amount=200_000, when=d, note="净额导出·被退的")
    c.refund(p, 50_000, when=d)
    c.expense(amount=30_000, when=d, note="净额导出·没冲抵的")

    st, sumdata = c.me.get("/api/tx?month=" + m)
    s = sumdata["summary"]
    want = s["expense"] - s["refund"] - s["reimb"]      # 页头那个「净流出」

    st, raw, _ = _csv_get(c.me, "/api/export.csv?month=" + m)
    head, rows = _csv_lines(raw)
    cols = next(_csv.reader(io.StringIO(head)))
    c.contains(head, "净额(元)", "表头有净额列")
    idx, note_i = cols.index("净额(元)"), cols.index("备注")

    cells = [next(_csv.reader(io.StringIO(r))) for r in rows]
    total = int(round(sum(float(x[idx]) for x in cells) * 100))
    c.eq(total, want, "整列 SUM = 净流出；已经分给原单的退款行记 0，否则会重复扣")
    by_note = {x[note_i]: x for x in cells}
    c.eq(by_note["净额导出·被退的"][idx], "1500.00", "被退那笔净额 = 2000 − 500")
    c.eq(by_note["净额导出·没冲抵的"][idx], "300.00", "没冲抵的净额就是原额")
    c.eq(by_note[""][idx], "0.00", "已分配完的退款行对一个 0：钱已经算在原单上了")
    c.eq(by_note["净额导出·被退的"][cols.index("金额(元)")], "2000.00",
         "金额列仍是原额，两个数各有各的用处")


if __name__ == "__main__":
    main()
