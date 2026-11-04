#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭账本 —— 单文件服务端（仅标准库）

设计要点
  * 两个人共用一本账，每人独立账号登录，每条记录带「归属 / 谁录的」
  * 金额全部以「分」为整数存储，避免浮点误差
  * 密码 PBKDF2-SHA256(200k)；会话为服务端随机 token，HttpOnly + SameSite=Lax
  * AI 记账：调用智谱 GLM，带 reasoning_effort=low（这是自研才有的话语权）
    GLM-5.3-Flash 是原生多模态，同一套接口既能啃一句话，也能啃支付截图/小票
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import signal
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(os.environ.get("LEDGER_HOME", "/opt/ledger"))
DB_PATH = BASE_DIR / "data" / "ledger.db"
HTML_PATH = BASE_DIR / "www" / "index.html"
# 图标 / 主屏幕资源：路径 → (相对 www 的文件, Content-Type)。
# 必须是**真实 URL 上的文件**——iOS 的桌面书签只认这个，data: URI 的图标
# 和 SVG 版 favicon 它都不吃，拿不到就退化成黑底首字的默认图。
STATIC_FILES = {
    "/apple-touch-icon.png": ("www/apple-touch-icon.png", "image/png"),
    "/icon-192.png": ("www/icon-192.png", "image/png"),
    "/icon-512.png": ("www/icon-512.png", "image/png"),
    "/favicon.svg": ("www/favicon.svg", "image/svg+xml"),
    "/manifest.webmanifest": ("www/manifest.webmanifest",
                              "application/manifest+json"),
}
PORT = int(os.environ.get("LEDGER_PORT", "9103"))
TZ = timezone(timedelta(hours=8))

SESSION_DAYS = 30
PBKDF2_ITER = 200_000

# ---------- AI ----------
# GLM-5.3-Flash 是原生多模态（1M 上下文，¥0.8/¥2.8 每百万 token），
# 文本与识图共用一套接口，不需要第二个厂商、第二套鉴权。
AI_DEFAULT_MODEL = "glm-5.3-flash"
AI_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
AI_MAX_IMAGE = 3 * 1024 * 1024      # 压缩后图片二进制上限，前端已缩到长边 1600
AI_LOG_KEEP_DAYS = 90               # 调用日志保留天数，防止表无限涨
# 每人每小时能问几次 AI。这是唯一按次花钱的功能，而入口是公开注册过的手机页面——
# 没有上限就等于「一个脚本能把额度刷干净」。60 次/小时对真人绰绰有余
# （识一张小票算一次，连着记一天也到不了），真撞上了通常是有东西在循环。
AI_QUOTA_PER_HOUR = 60
MAX_JSON_BODY = 4_200_000           # 带图的 JSON 会到几 MB（Caddy 那边限 8MB）
# 测试专用：置 1 时不发真实请求，返回固定草稿（只有 tests/ledger_suite.py 会设）
AI_MOCK = os.environ.get("LEDGER_AI_MOCK") == "1"

# ---------- 退款冲抵候选 ----------
# 实测（2026 全年账目）真实退款周期 1~6 天，30 天窗口留足 5 倍余量；
# 更早的账目走前端「搜更早的账单」，不再靠放大窗口硬捞。
REFUND_WINDOW_DAYS = 30
REFUND_TOL_FLOOR = 500      # 金额容差下限 5 元：小额单靠它（退 15 元 → 15~22.5）
REFUND_TOL_CAP = 20000      # 金额容差上限 200 元：大额单别放太宽
REFUND_TARGET_LIMIT = 8     # 候选最多返回几条，多了没法选

def word_to_cents(word: str):
    """把纯数字词解析成「分」，用于关键词的金额精确匹配；不是金额形态就返回 None。

    刻意不走 float：「12abc」那种会被 parseFloat 悄悄读成 12，这里一律不认。
    返回 0 也当没有（搜「0」没有意义）。
    """
    m = re.fullmatch(r"(\d{1,9})(?:\.(\d{1,2}))?", word or "")
    if not m:
        return None
    cents = int(m.group(1)) * 100 + int((m.group(2) or "").ljust(2, "0"))
    return cents or None


# 退款备注里这些词对匹配原单没有信息量，先剔掉再抽关键词
_REFUND_NOISE = ("退款", "退货", "退回", "返现", "全额", "部分", "退")
# 一笔冲抵记录分给原单多少钱（按归属看 offset_links，不看 parent_id —— 一次到账可能摊多笔）。
# 只出现在 SQL 片段里，不走参数：表名和列名都是常量，没有任何外部字符串拼进来。
OFFSET_USED_SQL = ("COALESCE((SELECT SUM(l.amount_cents) FROM offset_links l "
                   "WHERE l.parent_id=t.id),0)")

# 一笔冲抵记录里「已经分给支出」的金额（按 offset_id 看，与上面那个方向相反）
OFFSET_GIVEN_SQL = ("COALESCE((SELECT SUM(l.amount_cents) FROM offset_links l "
                    "WHERE l.offset_id=t.id),0)")

# 净流出（有符号）：支出取「原额 − 已分配冲抵」，退款/收入取负值——它们对流出是负贡献。
# 明细行的大数字、金额排序、金额搜索三处必须是同一个数，否则「屏幕上显示的」和
# 「排序/搜出来的」会各说各话。统计页的 expense 用的是同一套口径（见 _api_stats）。
NET_OUT_SQL = ("(CASE WHEN t.direction='expense' "
               "THEN t.amount_cents - " + OFFSET_USED_SQL + " "
               "ELSE -t.amount_cents END)")

# 前端只传键名，这里做白名单映射：外部字符串永远拼不进 SQL。
# 金额排序带上日期/id 兜底，同额记录的顺序才稳定（否则每次查询可能换位）。
TX_ORDERS = {
    "date_desc": "t.tx_date DESC, t.id DESC",
    "date_asc": "t.tx_date ASC, t.id ASC",
    # 「金额」= 净额，且带符号：退款/收入是负值，自然沉到高→低的末尾、低→高的开头。
    # 用原额排会让「全额退完」的那笔仍占榜首，和行里显示的数字自相矛盾。
    "amount_desc": NET_OUT_SQL + " DESC, t.tx_date DESC, t.id DESC",
    "amount_asc": NET_OUT_SQL + " ASC, t.tx_date DESC, t.id DESC",
}
TX_ORDER_DEFAULT = "date_desc"


def refund_keywords(note: str) -> list:
    """从退款备注里抽可用于匹配原单的关键词。

    「充电宝退款」→ ['充电宝']；「pdd手机壳退款」→ ['手机','机壳']。
    中文段取 2 字滑窗，避免「pdd手机壳」这种混排整段匹配不上；
    纯英文串（pdd / jd）保留原样。不引入分词依赖。
    """
    if not note:
        return []
    text = note
    for w in _REFUND_NOISE:
        text = text.replace(w, " ")
    out = []
    for seg in re.split(r"[^\u4e00-\u9fa5A-Za-z0-9]+", text):
        seg = seg.strip()
        if len(seg) < 2:
            continue
        if re.search(r"[\u4e00-\u9fa5]", seg):
            if len(seg) <= 4:
                out.append(seg)
            else:
                out.extend(p for p in (seg[i:i + 2] for i in range(len(seg) - 1))
                           if re.fullmatch(r"[\u4e00-\u9fa5]{2}", p))
        elif len(seg) >= 3:
            out.append(seg)
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq

# ---------- 分类（参考 ezBookkeeping 的两级设计）----------
# 大类顺序决定记账页胶囊顺序；颜色不跟顺序走（前端按类名固定），增删大类不会让别的类变色。
# 每个大类都留一个「其他」，迁移/随手记时细分不出来就落这里。
EXPENSE_TREE = {
    "餐饮": ["早餐", "午餐", "晚餐", "外卖", "买菜", "零食饮品", "其他"],
    "交通": ["公交地铁", "打车", "加油", "停车", "火车飞机", "其他"],
    "购物": ["日用品", "服饰", "数码", "家居", "其他"],
    "生活缴费": ["房租房贷", "物业", "水电燃气", "宽带", "话费", "会员订阅", "其他"],
    "医疗": ["门诊", "药品", "体检", "其他"],
    "娱乐": ["电影", "游戏", "旅行", "运动", "其他"],
    "人情": ["红包", "礼物", "请客", "其他"],
    "其他支出": ["其他", "课程", "书籍"],
}
INCOME_TREE = {
    "工资": ["月度工资", "奖金"],
    # 「报销」不该在收入里：垫出去的钱收回来是冲抵，不是收入（见 _reimburse）。
    # 真的是白给的钱（出差包干、餐补）才算收入，所以留「补贴」这一栏。
    "补贴": ["出差补贴", "餐补", "其他补贴"],
    "投资": ["利息", "理财收益"],
    "红包礼金": ["收红包", "礼金"],
    "其他收入": ["其他"],
}
REFUND_TREE = {
    "退款": ["购物退款", "服务退款", "其他退款"],
}
TREES = {"expense": EXPENSE_TREE, "income": INCOME_TREE, "refund": REFUND_TREE}

# 分类配置：DB 里存一份 JSON（三棵树 + 每个一级分类的色位号）。
# 上面这组硬编码只当「首次种子」用——配置记录不存在时种入一次，
# 之后管理员改什么都以 DB 为准，**发版更新默认树绝不能覆盖用户配置**。
# 全局变量，启动加载、保存时刷新；校验代码到处在读 TREES，保持只读引用。
CATEGORY_CONFIG_KEY = "category_config"
CATEGORY_LOG_KEEP = 200          # 分类修改日志滚动保留条数
CAT_COLORS = {k: {p: None for p in tree} for k, tree in TREES.items()}


def _default_category_config():
    """首次种子：把硬编码三棵树 + 现有 CAT_SLOT 色位转成配置 JSON 形状。"""
    seed_slot = {"expense": {"餐饮": 1, "交通": 2, "购物": 4, "生活缴费": 6,
                             "医疗": 3, "娱乐": 5, "人情": 7, "其他支出": 9}}
    out = {}
    for kind, tree in TREES.items():
        slots = seed_slot.get(kind, {})
        out[kind] = {p: {"color_slot": slots.get(p), "subs": list(subs)}
                     for p, subs in tree.items()}
    return out


def _load_category_config(conn):
    """启动 / 保存后刷新全局树与配色。读不到配置记录就种入默认值（只种一次）。"""
    global TREES, CAT_COLORS
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (CATEGORY_CONFIG_KEY,)).fetchone()
    if not row:
        cfg = _default_category_config()
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                     (CATEGORY_CONFIG_KEY, json.dumps(cfg, ensure_ascii=False)))
    else:
        try:
            cfg = json.loads(row["value"])
        except Exception:
            cfg = _default_category_config()
    TREES = {k: {p: list(v["subs"]) for p, v in cfg.get(k, {}).items()}
             for k in ("expense", "income", "refund")}
    CAT_COLORS = {k: {p: (v.get("color_slot") if isinstance(v, dict) else None)
                      for p, v in cfg.get(k, {}).items()}
                  for k in ("expense", "income", "refund")}


def _validate_category_config(cfg):
    """保存前校验。返回 (清洗后的配置, 错误信息)。错误信息为 None 表示通过。"""
    if not isinstance(cfg, dict):
        return None, "配置必须是对象"
    clean = {}
    for kind in ("expense", "income", "refund"):
        tree = cfg.get(kind)
        if not isinstance(tree, dict) or not tree:
            return None, f"{kind} 至少要有一个一级分类"
        ct = {}
        for parent, node in tree.items():
            parent = str(parent).strip()[:12]
            if not parent:
                return None, "一级分类名不能为空"
            if not isinstance(node, dict) or not isinstance(node.get("subs"), list) \
                    or not node["subs"]:
                return None, f"「{parent}」至少要有一个二级分类"
            subs = []
            for s in node["subs"]:
                s = str(s).strip()[:12]
                if not s:
                    return None, f"「{parent}」的子类名不能为空"
                if s in subs:
                    return None, f"「{parent}」下子类「{s}」重复"
                subs.append(s)
            slot = node.get("color_slot")
            if slot is not None and (not isinstance(slot, int) or not 0 <= slot <= 9):
                return None, f"「{parent}」色位号必须是 0~9"
            ct[parent] = {"color_slot": slot, "subs": subs}
        ct2 = {}
        for p, node in ct.items():
            if p in ct2:
                return None, f"{kind} 里一级分类「{p}」重复"
            ct2[p] = node
        clean[kind] = ct2
    return clean, None


def _category_log(conn, user_id, action, kind, parent, sub, detail, affected=0):
    """分类修改日志（滚动保留）。必须在业务写同一个 db() 事务里调用。"""
    conn.execute(
        "INSERT INTO category_log(ts,user_id,action,kind,cat_parent,category,detail,affected) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (now_iso(), user_id, action, kind, parent, sub, detail[:200], affected))
    conn.execute(
        "DELETE FROM category_log WHERE id NOT IN "
        "(SELECT id FROM category_log ORDER BY id DESC LIMIT ?)", (CATEGORY_LOG_KEEP,))

def _cat_usage(conn, kind, parent, sub=None):
    """某个分类（一级或子类）在历史账单里有多少笔。

    冲抵记录继承的是**支出的分类名**：改名时这些「影子行」也得跟着改，
    否则明细里那条冲抵还挂着已经不存在的旧分类名（R11 / D-03）。
    所以 expense 的用量要把 direction='refund' 的同名行算进去。
    """
    if sub:
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE direction=? "
                         "AND cat_parent=? AND category=?", (kind, parent, sub)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE direction=? "
                         "AND cat_parent=?", (kind, parent)).fetchone()[0]
    if kind == "expense":
        if sub:
            n += conn.execute("SELECT COUNT(*) FROM transactions WHERE direction='refund' "
                              "AND cat_parent=? AND category=?", (parent, sub)).fetchone()[0]
        else:
            n += conn.execute("SELECT COUNT(*) FROM transactions WHERE direction='refund' "
                              "AND cat_parent=?", (parent,)).fetchone()[0]
    return n


def _plan_category_change(conn, old_cfg, new_cfg):
    """算出「这次保存会做什么」，只读。

    返回 {"renames": [...], "deletes": [...], "blocked": [...], "affected_total": N}

    一级改名的判定（2026-10-30 收紧）：旧名的子类至少 2 个仍在新名里，
    且重合度 ≥ 0.6。只按「交集最大」配对，会把「删掉『餐饮』+ 新建『外卖』
    {外卖, 其他}」也判成改名——只因为两边都有个「其他」——于是 3 笔历史
    被静默改写到「外卖」名下（R3）。收紧之后这种情况落进「删除」，
    而删除有历史会被拦下，用户拿到的是明确提示，不是被改过的账。
    """
    plan = {"renames": [], "deletes": [], "blocked": [], "affected_total": 0}
    for kind in ("expense", "income", "refund"):
        old_tree = old_cfg.get(kind) or {}
        new_tree = new_cfg.get(kind) or {}
        gone_p = [p for p in old_tree if p not in new_tree]
        added_p = [p for p in new_tree if p not in old_tree]

        # ---- 一级改名 ----
        matched, used = {}, set()
        for op in gone_p:
            old_subs = set(old_tree[op].get("subs") or [])
            opts = []
            for np_ in added_p:
                if np_ in used:
                    continue
                new_subs = set(new_tree[np_].get("subs") or [])
                common = len(old_subs & new_subs)
                ratio = common / float(max(len(old_subs), len(new_subs)) or 1)
                if common >= 2 and ratio >= 0.6:
                    opts.append((common, ratio, np_))
            if not opts:
                continue
            opts.sort(key=lambda x: (-x[0], -x[1]))
            if len(opts) > 1 and opts[0][:2] == opts[1][:2]:
                continue        # 两个候选一模一样，不猜：宁可当删除+新增
            matched[op] = opts[0][2]
            used.add(opts[0][2])

        for op, np_ in matched.items():
            n = _cat_usage(conn, kind, op)
            plan["renames"].append({"kind": kind, "type": "parent", "parent": "",
                                    "from": op, "to": np_, "affected": n})
            plan["affected_total"] += n

        # ---- 一级删除 ----
        for op in gone_p:
            if op in matched:
                continue
            n = _cat_usage(conn, kind, op)
            item = {"kind": kind, "type": "parent", "parent": "", "from": op,
                    "to": "", "affected": n}
            (plan["blocked"] if n else plan["deletes"]).append(item)

        # ---- 子类改名 / 删除（父级还在的那些）----
        for p in new_tree:
            if p not in old_tree:
                continue
            old_subs = list(old_tree[p].get("subs") or [])
            new_subs = list(new_tree[p].get("subs") or [])
            gone_s = [s for s in old_subs if s not in new_subs]
            added_s = [s for s in new_subs if s not in old_subs]
            if len(gone_s) == 1 and len(added_s) == 1:
                n = _cat_usage(conn, kind, p, gone_s[0])
                plan["renames"].append({"kind": kind, "type": "sub", "parent": p,
                                        "from": gone_s[0], "to": added_s[0],
                                        "affected": n})
                plan["affected_total"] += n
                continue
            for s in gone_s:
                n = _cat_usage(conn, kind, p, s)
                item = {"kind": kind, "type": "sub", "parent": p, "from": s,
                        "to": "", "affected": n}
                (plan["blocked"] if n else plan["deletes"]).append(item)
    return plan


def _cat_label(item):
    return f"{item['parent']} / {item['from']}" if item["type"] == "sub" else item["from"]


def _blocked_msg(plan):
    """删除被拦下时的文案：点名是哪几个分类、各有几笔，并给出可执行的下一步。"""
    shown = plan["blocked"][:3]
    body = "、".join(f"「{_cat_label(b)}」{b['affected']} 笔" for b in shown)
    if len(plan["blocked"]) > len(shown):
        body += "等 %d 个分类" % len(plan["blocked"])
    return ("这些分类下还有历史账单，不能直接删：" + body +
            "。想换名字请改名（子类保持一致即可），想合并请先把这些账改到目标分类。")


def _apply_category_plan(conn, plan, u):
    """按计划改历史账单并写分类日志。调用方负责配置与事务。"""
    for r in plan["renames"]:
        if r["type"] == "parent":
            conn.execute("UPDATE transactions SET cat_parent=? WHERE direction=? AND cat_parent=?",
                         (r["to"], r["kind"], r["from"]))
            if r["kind"] == "expense":
                # 冲抵记录的分类是支出分类的副本，一起改（R11）
                conn.execute("UPDATE transactions SET cat_parent=? "
                             "WHERE direction='refund' AND cat_parent=?",
                             (r["to"], r["from"]))
            _category_log(conn, u["id"], "rename", r["kind"], r["from"], "",
                          f"{r['from']} → {r['to']}", r["affected"])
        else:
            conn.execute("UPDATE transactions SET category=? WHERE direction=? "
                         "AND cat_parent=? AND category=?",
                         (r["to"], r["kind"], r["parent"], r["from"]))
            if r["kind"] == "expense":
                conn.execute("UPDATE transactions SET category=? WHERE direction='refund' "
                             "AND cat_parent=? AND category=?",
                             (r["to"], r["parent"], r["from"]))
            _category_log(conn, u["id"], "rename", r["kind"], r["parent"], r["from"],
                          f"{r['parent']} / {r['from']} → {r['to']}", r["affected"])



# ---------- 数据库 ----------
_DB_LOCK = threading.RLock()
_DB_CONN = None

# 表结构只在这里写一份。新库直接按它建，老库由 _migrate_schema 重建到同一形状。
# 之所以要「同一份」：以前 CREATE TABLE IF NOT EXISTS 里一份、迁移里再拼一份，
# 加列时改了这头忘了那头，重建出来的表就少东西——而这种错偏偏只在恢复现场才暴露。
TRANSACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS {tbl} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_date TEXT NOT NULL
        CHECK (tx_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    direction TEXT NOT NULL
        CHECK (direction IN ('expense','income','refund')),
    cat_parent TEXT NOT NULL,
    category TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    shared INTEGER NOT NULL DEFAULT 0,
    reimbursed INTEGER NOT NULL DEFAULT 0,
    reimb_pending INTEGER NOT NULL DEFAULT 0,
    pending_use INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    travel INTEGER NOT NULL DEFAULT 0,
    offset_kind TEXT NOT NULL DEFAULT 'refund',
    client_token TEXT,
    updated_by INTEGER REFERENCES users(id)
)"""

# 列清单与上面的 DDL 逐字对应。重建时要显式列出：生产库老表的列顺序和 DDL 不一致
# （reimb_pending 等是后来 ALTER 加上去的），用 SELECT * 会按位置错列，且错得很安静。
# 写成列表而不是一行字符串，是因为重建时还要按「这张表实际有哪些列」过滤一遍——
# 新加的列在老库里不存在，直接 SELECT 会报 no such column。
TRANSACTIONS_COLS = [
    "id", "tx_date", "direction", "cat_parent", "category", "amount_cents", "shared",
    "reimbursed", "reimb_pending", "pending_use", "parent_id", "owner_id", "note",
    "created_by", "created_at", "updated_at", "travel", "offset_kind", "client_token",
    "updated_by",
]

# 索引单独一份：重建表时 DROP 会把索引一起删掉，必须照着这张清单重建，
# 漏一个就是「功能正常但慢了十倍」，没人会注意到。
# 用列表而不是一段 executescript：executescript 会先提交当前事务，把重建过程切成两半。
TRANSACTIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tx_date ON {tbl}(tx_date)",
    "CREATE INDEX IF NOT EXISTS idx_tx_owner ON {tbl}(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_tx_pending ON {tbl}(reimb_pending)",
    "CREATE INDEX IF NOT EXISTS idx_tx_parent ON {tbl}(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_tx_pending_use ON {tbl}(pending_use)",
    "CREATE INDEX IF NOT EXISTS idx_tx_travel ON {tbl}(travel)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_client_token ON {tbl}(client_token) "
    "WHERE client_token IS NOT NULL",
]

OFFSET_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS {tbl} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offset_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at TEXT NOT NULL,
    CHECK (offset_id <> parent_id),
    UNIQUE (offset_id, parent_id)
)"""

OFFSET_LINKS_COLS = ["id", "offset_id", "parent_id", "amount_cents", "created_at"]

OFFSET_LINKS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_off_links_off ON {tbl}(offset_id)",
    "CREATE INDEX IF NOT EXISTS idx_off_links_parent ON {tbl}(parent_id)",
]

# 删除审计：只记删除（最有价值的那一半），删之前把整行 + 它的冲抵分配一起存下来。
# 为什么连分配一起存：删掉一条冲抵时，offset_links 由 ON DELETE CASCADE 一并清空，
# 光有 transactions 那一行，事后根本不知道它当时抵了哪几笔、各多少——等于记了个空快照。
AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    tx_id INTEGER,
    before_json TEXT NOT NULL DEFAULT '',
    after_json TEXT NOT NULL DEFAULT ''
)"""

AUDIT_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_tx ON audit_log(tx_id)",
]

# 版本号只在表结构变化时 +1。每次升级写成一个独立函数，按序跑一遍。
SCHEMA_VERSION = 2


def _schema_version(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _set_schema_version(conn, v):
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version',?)", (str(v),))


def _conn():
    """进程内复用一条连接。

    这个服务是「两个人 + 一台小机器」，并发极低，共享连接最省事也最快：
    每次请求新建连接、关连接会触发 WAL checkpoint（实测单请求从 2ms 涨到 55ms，
    写入甚至会等锁超时）。用一把可重入锁把所有访问串起来即可。
    """
    global _DB_CONN
    if _DB_CONN is None:
        c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA journal_mode=WAL")   # 持久设置，首次连接时设一次
        _DB_CONN = c
    return _DB_CONN


@contextmanager
def db():
    """取连接：正常结束提交、出错回滚，锁在退出时释放。"""
    conn = _conn()
    with _DB_LOCK:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                pwd_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # transactions 的建表语句来自 TRANSACTIONS_DDL（唯一一份），不再在这里抄一遍
        conn.executescript(TRANSACTIONS_DDL.format(tbl="transactions"))
        # 轻量迁移：待报销标记（reimbursed 表示已到账，两者互斥）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
        if "reimb_pending" not in cols:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN reimb_pending INTEGER NOT NULL DEFAULT 0")
        # 退款冲抵：parent_id 只在退款行上有值，指向被冲抵的支出
        if "parent_id" not in cols:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN parent_id INTEGER "
                "REFERENCES transactions(id) ON DELETE SET NULL")
        # 待用券：买了还没核销的团购券/预付卡
        if "pending_use" not in cols:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN pending_use INTEGER NOT NULL DEFAULT 0")
        # 旅行标记：把一个行程的吃住行串起来，分类仍按真实属性记
        if "travel" not in cols:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN travel INTEGER NOT NULL DEFAULT 0")
        # 冲抵类型：direction='refund' 的行分两种——'refund' 是退货退款，'reimb' 是报销到账。
        # 两者在统计里的作用完全一样（都当负支出），分开只为报表能各说各的：
        # 「退款」卡不该混进「报销到账」，明细里也得说清这条冲的是退货还是回款。
        if "offset_kind" not in cols:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN offset_kind TEXT NOT NULL DEFAULT 'refund'")
        # 幂等键：客户端为「这一次提交」生成一个随机串，落账成功后换新的。
        # 关键是它标识的是**动作**而不是业务内容——同一天两笔都是 16.79 的打车，
        # 是两个不同的 token，照常各记一条；而双击/断网重发/结果未知时再点一次，
        # 复用的是同一个 token，只会落一条。拿金额日期分类去判重才是错的。
        # 老数据和内部生成的记录（冲抵分配、差补）不带 token，所以索引用 WHERE 只约束非空值。
        if "client_token" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN client_token TEXT")
        # 索引统一在列都齐了之后建：清单只有 TRANSACTIONS_INDEXES 一份，重建表时照它恢复
        for stmt in TRANSACTIONS_INDEXES:
            conn.execute(stmt.format(tbl="transactions"))
        # 冲抵分配：一条冲抵可以抵多笔支出——公司一次打回 900，对应的可能是 700+200 两张单。
        # 从此金额归属一律以本表为准；transactions.parent_id 只当「主原单」用于显示，
        # 统计、额度、状态全都不看它了（两处真相迟早打架，这个项目已经吃过一次亏）。
        conn.executescript(OFFSET_LINKS_DDL.format(tbl="offset_links"))
        for stmt in OFFSET_LINKS_INDEXES:
            conn.execute(stmt.format(tbl="offset_links"))
        # 一次性迁移：老数据每条冲抵只挂了 parent_id，补一条等额分配记录，语义不变
        if not conn.execute("SELECT 1 FROM settings WHERE key='migr_offset_links'").fetchone():
            conn.execute(
                "INSERT INTO offset_links(offset_id,parent_id,amount_cents,created_at) "
                "SELECT id, parent_id, amount_cents, created_at FROM transactions "
                "WHERE direction='refund' AND parent_id IS NOT NULL "
                "AND NOT EXISTS(SELECT 1 FROM offset_links l WHERE l.offset_id=transactions.id)")
            conn.execute("INSERT INTO settings(key,value) VALUES('migr_offset_links','1')")
        conn.execute("""CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,            -- out 借出（别人欠我）/ in 借入（我欠别人）
                counterparty TEXT NOT NULL,         -- 对方
                amount_cents INTEGER NOT NULL,
                repaid_cents INTEGER NOT NULL DEFAULT 0,
                loan_date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                owner_id INTEGER NOT NULL REFERENCES users(id),
                settled_at TEXT,                    -- 还清时间，空了就是还没还完
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        # AI 调用日志：换模型、调 prompt 时唯一的事实来源。
        # 只存输入摘要（前 120 字），不存整段原文——账本备注里有医院、人名这类信息。
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- text 一句话 / image 拍照识图
                model TEXT NOT NULL,
                user_id INTEGER,
                input_brief TEXT NOT NULL DEFAULT '',
                image_bytes INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,               -- ok / error
                error TEXT NOT NULL DEFAULT '',
                draft TEXT NOT NULL DEFAULT ''
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_log_ts ON ai_log(ts)")
        # 分类修改日志：谁在何时增删改/改色了哪个分类，改名连带改了多少笔账。
        # 滚动保留 CATEGORY_LOG_KEEP 条，写入时顺手裁剪。
        conn.execute("""CREATE TABLE IF NOT EXISTS category_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                user_id INTEGER,
                action TEXT NOT NULL,               -- add / del / rename / color / update
                kind TEXT NOT NULL,                 -- expense / income / refund
                cat_parent TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                affected INTEGER NOT NULL DEFAULT 0 -- 改名连带更新的账单笔数
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cat_log_id ON category_log(id)")
        # 分类配置：启动时加载（首次自动种入默认树），刷新全局 TREES / CAT_COLORS
        _load_category_config(conn)
        # 一次性迁移：默认模型 glm-5.3-flashx → glm-5.3-flash（原生多模态，价格低一半以上）。
        # 用 settings 里的标记保证只跑一次，之后管理员自己改成什么都覆盖不了。
        if not conn.execute("SELECT 1 FROM settings WHERE key='migr_ai_model_53'").fetchone():
            conn.execute("UPDATE settings SET value=? WHERE key='ai_model' AND value='glm-5.3-flashx'",
                         (AI_DEFAULT_MODEL,))
            conn.execute("INSERT INTO settings(key,value) VALUES('migr_ai_model_53','1')")
        # 一次性迁移：收入树里的「报销」换成「补贴」。
        # 垫出去的钱收回来是冲抵（见 _reimburse），不是收入；真正白给的钱（出差包干、餐补）才算收入。
        # 为什么不能只改 INCOME_TREE：那份硬编码只是「首次种子」，已有库的分类树存在
        # settings.category_config 里，改默认值对老库一律无效——这里踩过一次，行为看似改了其实没生效。
        if not conn.execute("SELECT 1 FROM settings WHERE key='migr_income_subsidy'").fetchone():
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (CATEGORY_CONFIG_KEY,)).fetchone()
            cfg = None
            if row:
                try:
                    cfg = json.loads(row["value"])
                except Exception:
                    cfg = None
            income = (cfg or {}).get("income")
            if isinstance(income, dict) and "报销" in income:
                node = income["报销"]
                slot = node.get("color_slot") if isinstance(node, dict) else None
                # 按原顺序重建，遇到「报销」原地换成「补贴」，胶囊顺序不变
                rebuilt = {}
                for name, item in income.items():
                    if name == "报销":
                        if "补贴" not in income:
                            rebuilt["补贴"] = {"color_slot": slot,
                                               "subs": ["出差补贴", "餐补", "其他补贴"]}
                    else:
                        rebuilt[name] = item
                cfg["income"] = rebuilt
                conn.execute("UPDATE settings SET value=? WHERE key=?",
                             (json.dumps(cfg, ensure_ascii=False), CATEGORY_CONFIG_KEY))
                _load_category_config(conn)     # 让全局 TREES 立刻反映新树
            conn.execute("INSERT INTO settings(key,value) VALUES('migr_income_subsidy','1')")
        cleanup_sessions(conn, force=True)
    _migrate_schema()


def _migrate_schema():
    """按 settings.schema_version 顺序升级表结构。启动时跑一次。

    为什么要版本号：以前每加一次迁移就往 settings 塞一个 migr_xxx 标记，加到第 8 个
    只能靠 grep 才知道这个库跑到哪一步，也分不清「全新库」和「跑到一半」。现在只看一个数字。
    每次升级写成一个独立函数，按序调用；老标记保留不动，作为兜底。
    """
    conn = _conn()
    with _DB_LOCK:
        v = _schema_version(conn)
        if v >= SCHEMA_VERSION:
            return
        # 按版本号从小到大走，一步失败就停下（后面的步骤依赖前一步的表结构）
        for target, fn, what in ((1, _migrate_v1_checks, "transactions/offset_links 带 CHECK"),
                                 (2, _migrate_v2_audit, "updated_by + 删除审计表")):
            if v >= target:
                continue
            if not fn(conn):
                # 跳过或已回滚：版本号停在这一步，下次启动会再试。服务保持可用。
                print("[schema] 停在 v%d，未升到 v%d（%s），服务照常运行"
                      % (v, target, what), flush=True)
                return
            v = target
            print("[schema] 已升到 v%d：%s" % (v, what), flush=True)


def _migrate_v1_checks(conn):
    """v1：给 transactions / offset_links 加 CHECK 约束。返回 True 表示已达标。

    SQLite 加不了 CHECK，只能「建新表 → 拷数据 → 删旧表 → 改名」。官方文档强调
    的三件事这里都做了：关外键、整体一个事务、重建索引。另外自己补了一条
    「恢复 sqlite_sequence」——不恢复的话删过的 id 会被新记录复用，
    而 parent_id 和 offset_links 可能正指着那个号，等于把两笔账悄悄串起来。
    """
    # 新库建表时就是带约束的（DDL 只有 TRANSACTIONS_DDL 一份），别再白重建一次
    tx_sql = (conn.execute("SELECT sql FROM sqlite_master WHERE name='transactions'")
              .fetchone() or [""])[0]
    if "CHECK" in tx_sql:
        _set_schema_version(conn, 1)
        conn.commit()
        return True

    # 先看脏数据。有脏的**就不迁移**：硬上必然约束失败、进程起不来，账一笔也记不了。
    # 更要紧的是——它只发生在启动阶段，用户看到的现象是「网站打不开了」，很难联想到数据。
    bad = conn.execute(
        "SELECT COUNT(*) FROM transactions "
        " WHERE direction NOT IN ('expense','income','refund') OR amount_cents<=0 "
        "    OR tx_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'").fetchone()[0]
    bad += conn.execute(
        "SELECT COUNT(*) FROM offset_links WHERE amount_cents<=0 OR offset_id=parent_id").fetchone()[0]
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM offset_links "
        " GROUP BY offset_id,parent_id HAVING COUNT(*)>1)").fetchone()[0]
    if bad or dup:
        print("[schema] 跳过 v1：%d 行不满足约束、%d 组重复分配。"
              "先跑 tools/repair-ledger-20261030.py 修干净再重启。" % (bad, dup), flush=True)
        return False

    def seq_of(name):
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    seq_tx, seq_lk = seq_of("transactions"), seq_of("offset_links")
    n_tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    sum_tx = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transactions").fetchone()[0]
    n_lk = conn.execute("SELECT COUNT(*) FROM offset_links").fetchone()[0]

    iso = conn.isolation_level
    conn.isolation_level = None         # 接管事务：默认模式下 DDL 会逐条自动提交，切碎了就没法回滚
    try:
        conn.execute("PRAGMA foreign_keys=OFF")     # 必须在 BEGIN 前；否则 DROP 旧表会连带删光 offset_links
        conn.execute("BEGIN IMMEDIATE")

        # ---- transactions ----
        conn.execute("DROP TABLE IF EXISTS transactions_new")
        conn.execute(TRANSACTIONS_DDL.format(tbl="transactions_new"))
        # 按老表**实际有的列**拷：新加的列（如 updated_by）在老库里不存在，直接 SELECT 会报错
        have = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
        cols = ",".join(c for c in TRANSACTIONS_COLS if c in have)
        conn.execute("INSERT INTO transactions_new (%s) SELECT %s FROM transactions"
                     % (cols, cols))
        conn.execute("DROP TABLE transactions")
        conn.execute("ALTER TABLE transactions_new RENAME TO transactions")
        for stmt in TRANSACTIONS_INDEXES:
            conn.execute(stmt.format(tbl="transactions"))

        # ---- offset_links ----
        conn.execute("DROP TABLE IF EXISTS offset_links_new")
        conn.execute(OFFSET_LINKS_DDL.format(tbl="offset_links_new"))
        have = {r["name"] for r in conn.execute("PRAGMA table_info(offset_links)")}
        cols = ",".join(c for c in OFFSET_LINKS_COLS if c in have)
        conn.execute("INSERT INTO offset_links_new (%s) SELECT %s FROM offset_links"
                     % (cols, cols))
        conn.execute("DROP TABLE offset_links")
        conn.execute("ALTER TABLE offset_links_new RENAME TO offset_links")
        for stmt in OFFSET_LINKS_INDEXES:
            conn.execute(stmt.format(tbl="offset_links"))

        # 自增序列调回旧值（只在旧值更大时生效）。丢掉它会复用已删的 id。
        for name, old in (("transactions", seq_tx), ("offset_links", seq_lk)):
            if old:
                conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name=? AND seq<?",
                             (old, name, old))

        # 拷完立刻自查，不合格就整体回滚——宁可这次不升，也不能留一个半截的库
        chk = (conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
               conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transactions").fetchone()[0],
               conn.execute("SELECT COUNT(*) FROM offset_links").fetchone()[0])
        if chk != (n_tx, sum_tx, n_lk):
            raise RuntimeError("重建后对不上：期望 %s，实际 %s" % ((n_tx, sum_tx, n_lk), chk))

        _set_schema_version(conn, 1)
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print("[schema] v1 升级失败，已回滚，表结构保持原样：%s" % e, flush=True)
        return False
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = iso

    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    print("[schema] v1 完成：%d 笔 / %d 分配，外键异常 %d 条" % (n_tx, n_lk, len(broken)),
          flush=True)
    return True


def _migrate_v2_audit(conn):
    """v2：updated_by + 删除审计表。都是向后兼容的加列/建表，失败了也只是少个功能。

    为什么要 updated_by：两个人一本账，出了「这笔记怎么变了」现在完全查不到是谁动的。
    只记「最后一次改的人」，不记每一次改动——夫妻账本不是审计系统，留一个可问的人就够。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "updated_by" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN updated_by INTEGER REFERENCES users(id)")
    conn.execute(AUDIT_LOG_DDL)
    for stmt in AUDIT_LOG_INDEXES:
        conn.execute(stmt)
    _set_schema_version(conn, 2)
    conn.commit()
    return True


def audit_delete(conn, u, kind, row, extra=None):
    """把「即将被删的那一行」完整存下来。删之前调用。

    extra 用来塞那些会连带消失的东西（比如这条冲抵的分配明细），
    不然删完只剩一句「某天删了一笔」，等于没记。
    """
    snap = dict(row) if row is not None else {}
    if extra:
        snap.update(extra)
    conn.execute(
        "INSERT INTO audit_log(ts,user_id,username,action,kind,tx_id,before_json,after_json) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (now_iso(), u["id"], u["username"], "delete", kind, snap.get("id"),
         json.dumps(snap, ensure_ascii=False, default=str), ""))


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


# ---------- 请求参数解析 ----------
# 建账（_create_tx）与改账（_api_patch）曾经各写一套校验：同一个字段两个口径，
# 严的那个有测试盯着、松的那个没有，于是「编辑时把日期清空」「PATCH 金额 2 亿元」
# 这类问题只能从改账那条路进来（2026-10-29 审查 R1/R2/R9）。
# 下面这些函数是唯一口径，两个入口都必须走它们。

class FieldError(ValueError):
    """字段校验失败，自带稳定错误码。

    前端按 code 分支（`date_required` / `amount_invalid` …），不按中文文案分支——
    文案随时会改，码才是契约。仍然是 ValueError，老调用方 `except ValueError` 照常接得住。
    """

    def __init__(self, msg, code):
        ValueError.__init__(self, msg)
        self.code = code


def parse_opt_int(v, field="参数"):
    """可选的整数 id：None / 空串 / 0 → None，其余必须是整数。

    不接受容器（`[]`、`{}`）和布尔——它们 `int()` 出来是 0/1，
    静默变成「解除冲抵」或「归属 1 号用户」比报错更难查。
    """
    if isinstance(v, bool) or isinstance(v, (float, dict, list)):
        raise ValueError("%s不合法" % field)
    if v is None or v == "" or v == 0:
        return None
    try:
        n = int(v)
    except Exception:
        raise ValueError("%s不合法" % field)
    if n <= 0:
        return None
    return n


def parse_tx_date(v, default=None):
    """把用户传来的日期收敛成 'YYYY-MM-DD'。

    空值：给了 default 就返回 default（建账不填 = 今天），否则抛错。
    非法值一律抛错，绝不"兜底成今天"——编辑时把日期清空如果悄悄变成今天，
    用户看到的是"我明明改了别的字段，怎么这笔账跑到今天来了"。
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        if default is not None:
            return default
        raise FieldError("日期不能为空", "date_required")
    if isinstance(v, (bool, dict, list)):
        raise FieldError("日期格式应为 YYYY-MM-DD", "date_invalid")
    s = str(v).strip()
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise FieldError("日期格式应为 YYYY-MM-DD", "date_invalid")
    # strptime 认 "2026-1-5" 这种少补零的写法，也认 "2026-02-31"→ 会报错，不用额外判。
    if not (date(2000, 1, 1) <= d <= date(2100, 12, 31)):
        raise FieldError("日期超出允许范围", "date_invalid")
    return d.isoformat()


def month_bounds(month):
    """把 'YYYY-MM' 收敛成 [start, end) 两个日期串，让 tx_date 走索引。

    为什么不用 `tx_date LIKE '2026-10-%'`：LIKE 前面的通配位置决定了用不用得上索引，
    这种写法在默认配置下是**全表扫描**（数据涨上去写操作会被拖住，而写是全局锁）。
    闭开区间 `>= '2026-10-01' AND < '2026-11-01'` 才能命中 idx_tx_date。

    返回 None 表示「不限月份」；格式不对时返回一对不可能命中的边界，
    保持「参数写错就查不到」的旧语义——绝不能因为参数错就把整本账倒出来。
    跨年不手写「月份 +1」，一律用 date 算，12 月才不会变成 13 月。
    """
    if not month:
        return None
    mm = re.fullmatch(r"(\d{4})-(\d{2})", str(month).strip())
    if not mm:
        return ("0000-00-00", "0000-00-00")
    y, mo = int(mm.group(1)), int(mm.group(2))
    if not (1 <= mo <= 12) or not (2000 <= y <= 2100):
        return ("0000-00-00", "0000-00-00")
    start = date(y, mo, 1)
    end = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
    return (start.isoformat(), end.isoformat())


def like_escape(s):
    """把 LIKE 的通配符当普通字符：搜「50%」就该只找到写着 50% 的备注。

    以前直接拼 `%word%`，用户搜 `%` 或 `_` 会变成通配，搜「_」等于搜任意一个字，
    结果「搜什么都没差」。三处 LIKE 全部配 ESCAPE '\\' 使用。
    """
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# 标记条件抽成表：要（mark）和不要（exclude）共用同一段 SQL，避免两处写法漂移
MARK_SQL = {
    "shared": "t.shared=1",
    "pending": "t.reimb_pending=1",
    "reimb": "t.reimbursed=1",
    "voucher": "t.pending_use=1",
    "travel": "t.travel=1",
    # 只认退货退款，不含报销冲抵——两者在明细里是两种事，筛一起会分不清
    "refunded": "EXISTS(SELECT 1 FROM offset_links l "
                "JOIN transactions x ON x.id=l.offset_id "
                "WHERE l.parent_id=t.id AND x.offset_kind='refund')",
}


def tx_filter(qs):
    """把明细的查询参数编译成 (WHERE 子句, 参数表, ORDER BY)。

    为什么必须只有这一份：导出 CSV 和明细列表各写一套过滤，两边的「筛选」就会慢慢
    长成两个东西——用户勾了「待报销」导出，文件里却混着别的，比没有导出更糟。
    「导出行数 = 页面上的 count」这条验收，靠的是共用而不是各测一遍。

    容错风格统一为「参数不对就当没传」：解析失败绝不只拼占位符不加参数——
    那会让整条 SQL 参数不匹配（曾造成整个请求断连），也绝不能因为参数写错
    就把整本账倒出来。
    """

    def one(k):
        return (qs.get(k) or [""])[0]

    month, owner, direction = one("month"), one("owner"), one("direction")
    mark, cat_parent, kw = one("mark"), one("cat_parent"), one("q").strip()
    # 起点放一个恒真条件，保证 " AND ".join(where) 永远不为空字符串（空 WHERE 是语法错）
    where = ["1=1"]
    args = []
    mb = month_bounds(month)
    if mb:
        where += ["t.tx_date>=?", "t.tx_date<?"]
        args += [mb[0], mb[1]]
    if owner:
        # 先解析再拼 SQL：解析失败就当作没传，绝不能只加占位符不加参数
        try:
            owner_id = int(owner)
        except ValueError:
            owner_id = None
        if owner_id is not None:
            where.append("t.owner_id=?")
            args.append(owner_id)
    if direction:
        where.append("t.direction=?")
        args.append(direction)
    if cat_parent:
        where.append("t.cat_parent=?")
        args.append(cat_parent)

    def mark_cond(raw):
        # 逗号分隔多选：要的之间是「或」（一起勾「待报销 + 已报销」才不会互相抵消），
        # 不认识的键直接丢掉，与 owner / min_cents 的容错风格一致
        keys = [k for k in raw.split(",") if k in MARK_SQL]
        if not keys:
            return None
        return "(" + " OR ".join(MARK_SQL[k] for k in keys) + ")"

    wanted = mark_cond(mark)
    if wanted:
        where.append(wanted)
    banned = mark_cond(one("exclude"))
    if banned:
        where.append("NOT " + banned)
    # 日期区间：闭区间，含首尾两天。格式非法就当没传（与 owner / min_cents 同样的容错风格）
    for key, op in (("start", ">="), ("end", "<=")):
        raw = one(key)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            where.append("t.tx_date%s?" % op)
            args.append(raw)
    if kw:
        # 空格分隔多个词，全部 AND（「三亚 机票」= 两个词都要命中）。
        # 每个词在「备注 / 子类 / 大类 / 支出人」里任一命中即算，纯数字再额外匹配金额。
        # 人名用子查询而不是 u.display_name：summary 聚合没有 JOIN users，
        # 两处共用同一段 clause，这里必须两种查询都能跑。
        for word in kw.split():
            like = "%" + like_escape(word) + "%"
            hit = ["t.note LIKE ? ESCAPE '\\'", "t.category LIKE ? ESCAPE '\\'",
                   "t.cat_parent LIKE ? ESCAPE '\\'",
                   "t.owner_id IN (SELECT id FROM users "
                   "WHERE display_name LIKE ? ESCAPE '\\')"]
            args.extend([like, like, like, like])
            amt = word_to_cents(word)
            if amt:
                # 明细行显示的是净额，屏幕上看到的数字必须搜得到；原额照旧能搜
                # （退了 900 的 1000 元那笔，搜 1000 和搜 100 都能找到它）。
                hit.append("t.amount_cents=?")
                args.append(amt)
                hit.append(NET_OUT_SQL + "=?")
                args.append(amt)
            where.append("(" + " OR ".join(hit) + ")")
    for key, op in (("min_cents", ">="), ("max_cents", "<=")):
        raw = one(key)
        if not raw:
            continue
        # 先转成数字，成功才拼 SQL。否则「加了占位符却没加参数」会让整条查询报错
        try:
            limit = int(raw)
        except ValueError:
            continue
        where.append("t.amount_cents%s?" % op)
        args.append(limit)
    # 排序键只查表，不拼接：非法值静默回落到默认日期倒序
    order = TX_ORDERS.get(one("sort"), TX_ORDERS[TX_ORDER_DEFAULT])
    return " AND ".join(where), args, order


AMOUNT_MAX_CENTS = 100_000_000       # 100 万元，与前端 AMOUNT_MAX_CENTS 同源


def parse_cents(v, field="金额"):
    """把用户传来的金额收敛成正整数「分」。

    上限只在这里写一次：建账曾有 100 万的上限、改账没有，于是 PATCH 能塞进 2 亿元
    （2026-10-29 审查 R9）。统计是按分累加的，一个 2 亿元的记录能把整月口径毁掉。
    """
    if isinstance(v, bool) or isinstance(v, (float, dict, list)):
        raise FieldError("%s不合法" % field, "amount_invalid")
    try:
        c = int(str(v).strip())
    except Exception:
        raise FieldError("%s不合法" % field, "amount_invalid")
    if c <= 0:
        raise FieldError("%s必须大于 0" % field, "amount_invalid")
    if c > AMOUNT_MAX_CENTS:
        raise FieldError("%s不能超过 100 万元" % field, "amount_invalid")
    return c


# ---- 冲抵分配（offset_links）----
# 一条冲抵（退款 / 报销到账）可以摊在多笔支出上：公司一次打回 900，对应的是 700 + 200 两张单。
# 金额归属只看这张表，transactions.parent_id 仅作「主原单」用于列表显示。

def offset_used(conn, parent_id):
    """这笔支出已经被冲抵掉多少（退货 + 报销合计）。"""
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM offset_links WHERE parent_id=?",
        (parent_id,)).fetchone()[0]


def check_links(conn, offset_id, offset_cents, pairs):
    """校验一份分配方案。返回 (清洗后的 [(parent_id, cents)], 错误信息)。"""
    merged = {}
    for pid, cents in pairs:
        if pid == offset_id:
            return None, "不能冲抵自己"
        if cents <= 0:
            return None, "每笔分配金额必须大于 0"
        merged[pid] = merged.get(pid, 0) + cents
    if sum(merged.values()) > offset_cents:
        return None, "分配总额超过了这笔冲抵的金额"
    for pid, cents in merged.items():
        orig = conn.execute("SELECT * FROM transactions WHERE id=?", (pid,)).fetchone()
        if not orig or orig["direction"] != "expense":
            return None, "只能冲抵支出记录"
        # 别的冲抵已经占用的额度要扣掉，否则两笔冲抵能各自把同一笔支出冲满
        others = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM offset_links "
            "WHERE parent_id=? AND offset_id<>?", (pid, offset_id)).fetchone()[0]
        remain = orig["amount_cents"] - others
        if cents > remain:
            return None, ("%s 那笔最多还能冲抵 %.2f 元（原额 %.2f，已被冲 %.2f）"
                          % (orig["tx_date"], remain / 100.0,
                             orig["amount_cents"] / 100.0, others / 100.0))
    return list(merged.items()), None


def replace_links(conn, offset_id, pairs):
    """整体替换一条冲抵的分配。返回受影响的原单 id 集合（含被摘掉的旧原单）。"""
    old = {r["parent_id"] for r in conn.execute(
        "SELECT parent_id FROM offset_links WHERE offset_id=?", (offset_id,))}
    conn.execute("DELETE FROM offset_links WHERE offset_id=?", (offset_id,))
    ts = now_iso()
    for pid, cents in pairs:
        conn.execute(
            "INSERT INTO offset_links(offset_id,parent_id,amount_cents,created_at) "
            "VALUES(?,?,?,?)", (offset_id, pid, cents, ts))
    return old | {pid for pid, _ in pairs}


def sync_parent_state(conn, parent_ids):
    """分配变动后重算原单的报销状态。只处理支出。

    规则：有报销分配 → 已报销；报销分配全没了但之前报过 → 退回待报销。
    「已退金额 / 剩余可冲抵」都是算出来的（offset_used），不需要存。
    """
    ts = now_iso()
    for pid in parent_ids:
        if not pid:
            continue
        row = conn.execute("SELECT direction, reimbursed FROM transactions WHERE id=?",
                           (pid,)).fetchone()
        if not row or row["direction"] != "expense":
            continue
        has_reimb = conn.execute(
            "SELECT 1 FROM offset_links l JOIN transactions r ON r.id=l.offset_id "
            "WHERE l.parent_id=? AND r.offset_kind='reimb' LIMIT 1", (pid,)).fetchone()
        if has_reimb:
            conn.execute("UPDATE transactions SET reimbursed=1, reimb_pending=0, updated_at=? "
                         "WHERE id=?", (ts, pid))
        elif row["reimbursed"]:
            # 撤回最后一条报销：回到待报销，而不是变成普通账单
            conn.execute("UPDATE transactions SET reimbursed=0, reimb_pending=1, updated_at=? "
                         "WHERE id=?", (ts, pid))


def apply_reimb(conn, u, tx_date, total_cents, pairs, subsidy_cents, subsidy_cat,
                owner_fallback, note="", client_token=None):
    """一次报销到账：拆成「冲抵记录 + 差补收入」两条。

    total_cents    到账总额（银行/微信里那一个数）
    pairs          [(原单 id, 本次分配额)]，可以一笔都没有（全都还没认领）
    subsidy_cents  其中属于差补的部分 → 记收入，不冲抵
    剩下的（到账 − 分配合计 − 差补）落在冲抵记录里当「未分配」，按无主算在到账当月

    为什么冲抵和差补必须拆两条记录：钱的性质不同。冲抵是把垫出去的钱收回来（减支出），
    差补是公司白给的（增收入）。混在一条里，统计得把一条记录拆两半算；更要命的是
    「补贴」这个分类会被垫付收回的钱污染——那是年终想看「今年补贴拿了多少」的地方。

    归属一律跟原单走：差补是给出差那个人的，不是给记账那个人的。

    返回 (冲抵记录 id, 差补记录 id)，没有差补时第二个是 None。
    """
    if total_cents <= 0:
        raise ValueError("金额必须大于 0")
    if subsidy_cents < 0 or subsidy_cents >= total_cents:
        raise ValueError("差补金额必须小于到账总额")
    offset_cents = total_cents - subsidy_cents
    clean, err = check_links(conn, 0, offset_cents, pairs or [])
    if err:
        raise ValueError(err)
    clean = clean or []

    # 主原单只用于列表显示（「冲抵 10-27 交通」）：取分配额最大的那笔
    if clean:
        main_pid = max(clean, key=lambda x: x[1])[0]
        orig = conn.execute("SELECT cat_parent, category, owner_id FROM transactions "
                            "WHERE id=?", (main_pid,)).fetchone()
        cat_parent, cat, owner_id = orig["cat_parent"], orig["category"], orig["owner_id"]
    else:
        # 一笔都没认领：落退款树第一项兜底，用户看不见这条的分类（报销模式不显示分类卡）
        main_pid = None
        trees = TREES["refund"]
        cat_parent = "退款" if "退款" in trees else list(trees)[0]
        cat = trees[cat_parent][0]
        owner_id = owner_fallback

    ts = now_iso()
    # 幂等键只挂在冲抵记录上：它是这次提交的主记录，差补收入是它的附属行。
    # 两行带同一个 token 会直接撞唯一索引，反而把正常记账拦下来。
    cur = conn.execute(
        "INSERT INTO transactions(tx_date,direction,cat_parent,category,amount_cents,"
        "shared,reimbursed,reimb_pending,pending_use,travel,parent_id,owner_id,note,"
        "created_by,created_at,updated_at,offset_kind,client_token) "
        "VALUES(?,?,?,?,?,0,0,0,0,0,?,?,?,?,?,?,'reimb',?)",
        (tx_date, "refund", cat_parent, cat, offset_cents, main_pid, owner_id,
         (note or "报销到账")[:100], u["id"], ts, ts, client_token))
    refund_id = cur.lastrowid
    for pid, c in clean:
        conn.execute(
            "INSERT INTO offset_links(offset_id,parent_id,amount_cents,created_at) "
            "VALUES(?,?,?,?)", (refund_id, pid, c, ts))
    sync_parent_state(conn, [pid for pid, _ in clean])

    income_id = None
    if subsidy_cents > 0:
        trees = TREES["income"]
        if "补贴" not in trees:
            raise ValueError("收入分类里没有「补贴」，请先到分类设置加回来再记差补")
        subs = trees["补贴"]
        sub = subsidy_cat if subsidy_cat in subs else subs[0]
        cur2 = conn.execute(
            "INSERT INTO transactions(tx_date,direction,cat_parent,category,amount_cents,"
            "shared,reimbursed,reimb_pending,pending_use,travel,parent_id,owner_id,note,"
            "created_by,created_at,updated_at,offset_kind) "
            "VALUES(?,?,?,?,?,0,0,0,0,0,NULL,?,?,?,?,?,'refund')",
            (tx_date, "income", "补贴", sub, subsidy_cents, owner_id,
             ("差补 · 报销到账 %.2f 元的一部分" % (total_cents / 100.0))[:100],
             u["id"], ts, ts))
        income_id = cur2.lastrowid
    return refund_id, income_id


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------- 登录限速（内存，够用）----------
# 三个口子一起卡，缺一个就绕得过去：
#   pair 同一 IP + 同一账号（最常见的手输密码）
#   user 同一账号换 IP（对着某个账号慢慢猜）
#   ip   同一 IP 换账号（拿一个字典把用户名挨个试）
# 只按用户名限的话，换个 IP 就清零；只按 IP 限的话，换个用户名就清零。
ATTEMPT_WINDOW = 600          # 10 分钟
ATTEMPT_LIMITS = {"pair": 8, "user": 20, "ip": 40}
_attempts = {}                # key -> (count, first_ts)
_attempt_lock = threading.Lock()
_attempt_last_gc = 0.0


def _attempt_keys(ip, username):
    return (("pair", "%s|%s" % (ip, username)),
            ("user", "u:" + username),
            ("ip", "i:" + ip))


def _gc_attempts(now):
    """清掉过期的失败计数。

    以前只在「同一个 key 又试错一次」时才顺手删，于是一年没人碰过的 key 就一直躺在字典里。
    这是全服务唯一没有上界的结构，加个定期清扫不费事（每 60 秒最多扫一遍）。
    """
    global _attempt_last_gc
    if now - _attempt_last_gc < 60:
        return
    _attempt_last_gc = now
    for k in [k for k, (_, first) in _attempts.items() if now - first > ATTEMPT_WINDOW]:
        _attempts.pop(k, None)


def too_many_attempts(ip: str, username: str) -> bool:
    now = time.time()
    with _attempt_lock:
        for kind, key in _attempt_keys(ip, username):
            rec = _attempts.get(key)
            if not rec:
                continue
            count, first = rec
            if now - first > ATTEMPT_WINDOW:
                _attempts.pop(key, None)
                continue
            if count >= ATTEMPT_LIMITS[kind]:
                return True
    return False


def note_attempt(ip: str, username: str, ok: bool):
    with _attempt_lock:
        if ok:
            for _, key in _attempt_keys(ip, username):
                _attempts.pop(key, None)
            return
        now = time.time()
        _gc_attempts(now)
        for _, key in _attempt_keys(ip, username):
            count, first = _attempts.get(key, (0, now))
            if now - first > ATTEMPT_WINDOW:      # 窗口过了就重新算
                count, first = 0, now
            _attempts[key] = (count + 1, first)


# ---------- 会话清理 ----------
_last_session_gc = 0.0


def cleanup_sessions(conn, force=False):
    """删掉过期会话。启动时强制清一次，之后最多 6 小时一次。

    以前从不清：过期的 token 一直躺在 sessions 表里，等有人拿着它来查才发现作废。
    两个人 + 30 天有效期，数量本来就不大，但「留着已经失效的凭证」没有任何好处；
    真要清还得手工敲 SQL，不如让服务自己顺手做掉。
    """
    global _last_session_gc
    now = time.time()
    if not force and now - _last_session_gc < 6 * 3600:
        return 0
    _last_session_gc = now
    return conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),)).rowcount


# ---------- AI ----------
def ai_settings():
    with db() as conn:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}
    return rows.get("ai_api_key", ""), rows.get("ai_model") or AI_DEFAULT_MODEL


def ai_check_image(raw: str) -> str:
    """校验前端传来的图片，统一成 data URL 再交给模型。

    前端已经把图压到长边 1600 / JPEG 0.82（正常 200~500KB），这里再兜一层：
    既防有人直接 POST 一张 10MB 原图撑爆请求体，也防脏数据进到模型那里变成一个
    莫名其妙的 400。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    mime = "jpeg"
    m = re.match(r"^data:image/(jpeg|jpg|png|webp);base64,", raw)
    if m:
        mime = "jpeg" if m.group(1) in ("jpg", "jpeg") else m.group(1)
        raw = raw[m.end():]
    elif raw.startswith("data:"):
        raise ValueError("图片格式不支持，请用 JPG / PNG / WebP")
    b64 = re.sub(r"\s+", "", raw)
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        raise ValueError("图片数据损坏，请重新选一张")
    if len(blob) < 512:
        raise ValueError("这张图几乎是空的，可能没拍上")
    if len(blob) > AI_MAX_IMAGE:
        raise ValueError(f"图片太大（{len(blob) // 1024}KB），请压缩后再试")
    return f"data:image/{mime};base64,{b64}"


def ai_prompt(today: str, with_image: bool) -> str:
    cats = "\n".join(
        f"{kind}: " + "；".join(f"{p}({'/'.join(c)})" for p, c in tree.items())
        for kind, tree in TREES.items()
    )
    lines = [
        "你是家庭记账助手。把用户提供的输入解析成一条账单，只输出 JSON，不要任何解释。",
        "字段：",
        f"  tx_date: YYYY-MM-DD（没提到日期就用今天 {today}）",
        "  direction: expense / income / refund",
        "  cat_parent: 必须从下面的大类里选",
        "  category: 必须是该大类下的子类",
        "  amount: 数字（元，不带符号）",
        "  shared: true 表示家庭共同支出（房租、水电、买菜等），否则 false",
        "  reimb_pending: true 当用户说「待报销」「要拿去报销」，即垫付了还没到账",
        "  reimbursed: true 仅当用户说「已经报销了」「钱到账了」；reimb_pending 与 reimbursed 不可同时为 true",
        "  pending_use: true 当用户说「团购了一张券」「囤了券」这类还没消费的预付券",
        "  travel: true 当这句话明显发生在旅行/出差途中（如「三亚」「出差」「去机场」），",
        "          注意分类仍按真实属性选（吃饭就选餐饮），不要因为旅行就塞进「娱乐·旅行」",
        # 备注只准写「以后能想起这笔」的信息。原来只写「简短备注，可留空」，模型要么把整句
        # 复述一遍（「出差打车去机场，待报销」），要么干脆留空，前端再把原文塞进去——两头都不像备注。
        "  note: 简短备注（≤12 字）。写「以后能帮你想起这笔」的信息——买了什么、在哪、和谁、为什么，",
        "        例如「食品袋」「和同事吃饭」「楼下超市」「给妈妈买药」。",
        "        不要复述原句，不要写金额、日期、方向词（支出/花了/买了），也不要写标记（待报销/共同/旅行）",
        "        ——它们都有专门字段。只有当原句除了金额和分类确实没别的可说时，才留空字符串。",
    ]
    if with_image:
        lines += [
            "另外这张图是支付截图或购物小票，先看清图再填：",
            "  amount 取「实付金额」，不是优惠前原价、也不是找零",
            "  note 写商家名或买了什么，不要照抄整段小票",
            "  ocr_hint: 把你在图里读到的关键文字原样摘出来（≤60 字，含金额与商家），"
            "用户要靠它核对你有没有读错",
            "  图里若有多笔，只取最显眼的那一笔；实在读不出金额就填 0",
        ]
    lines.append("可选分类：")
    lines.append(cats)
    return "\n".join(lines)


def ai_normalize(draft: dict, today: str, with_image: bool) -> dict:
    """把模型输出洗干净：分类必须落在 TREES 里，标记只对支出生效。"""
    kind = draft.get("direction") if draft.get("direction") in TREES else "expense"
    tree = TREES[kind]
    parent = draft.get("cat_parent") or next(iter(tree))
    if parent not in tree:
        parent = next(iter(tree))
    cat = draft.get("category") or tree[parent][0]
    if cat not in tree[parent]:
        cat = tree[parent][0]
    try:
        amount = round(float(draft.get("amount") or 0), 2)
    except Exception:
        amount = 0

    pending = bool(draft.get("reimb_pending", False))
    reimbursed = bool(draft.get("reimbursed", False)) and not pending

    return {
        "tx_date": str(draft.get("tx_date") or today)[:10],
        "direction": kind,
        "cat_parent": parent,
        "category": cat,
        "amount_cents": int(round(amount * 100)),
        "shared": bool(draft.get("shared", False)),
        "reimb_pending": pending,
        "reimbursed": reimbursed,
        "pending_use": bool(draft.get("pending_use", False)) if kind == "expense" else False,
        "travel": bool(draft.get("travel", False)) if kind == "expense" else False,
        "note": str(draft.get("note") or "")[:100],
        # 识图时把「模型读到了什么」回给前端，用户能一眼核对金额有没有读错
        "ocr_hint": str(draft.get("ocr_hint") or "")[:80] if with_image else "",
    }


def _ai_send(body: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        AI_API_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read())


def _ai_mock_reply(text: str, with_image: bool) -> dict:
    """测试替身：不联网，返回形状与真实响应一致的草稿。

    只有 tests/ledger_suite.py 启动实例时会设 LEDGER_AI_MOCK=1；
    生产 systemd 里没有这个变量，走的永远是真实请求。
    """
    if "FAIL" in text:
        raise RuntimeError("AI 接口错误 401: mock failure")
    payload = {
        "tx_date": datetime.now(TZ).date().isoformat(),
        "direction": "expense",
        "cat_parent": "餐饮",
        "category": "午餐",
        "amount": 25,
        "note": (text[:20] or "mock") if not with_image else "超市",
        "ocr_hint": "微信支付 25.00 元" if with_image else "",
    }
    # 让用例能覆盖「模型编了个不存在的分类」这条兜底路径
    if "BADCAT" in text:
        payload["cat_parent"] = "不存在的大类"
        payload["category"] = "也不存在"
    return {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 60},
    }


def _data_url_bytes(url: str) -> int:
    try:
        return len(url.split(",", 1)[1]) * 3 // 4
    except Exception:
        return 0


def ai_log_write(kind, model, user_id, brief, image_bytes, usage,
                 status, error, draft, latency_ms):
    """调用日志。写失败绝不能影响记账本身，所以整段吞异常。"""
    try:
        cutoff = (datetime.now(TZ) - timedelta(days=AI_LOG_KEEP_DAYS)).isoformat(timespec="seconds")
        with db() as conn:
            conn.execute(
                "INSERT INTO ai_log(ts,kind,model,user_id,input_brief,image_bytes,latency_ms,"
                "prompt_tokens,completion_tokens,status,error,draft) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), kind, model or "-", user_id, brief or "", image_bytes, latency_ms,
                 int(usage.get("prompt_tokens") or 0),
                 int(usage.get("completion_tokens") or 0),
                 status, error or "",
                 json.dumps(draft, ensure_ascii=False) if draft else ""))
            # 顺手清过期：表很小，每次删一下比另起 cron 简单
            conn.execute("DELETE FROM ai_log WHERE ts < ?", (cutoff,))
    except Exception:
        pass


def ai_parse(text: str, image: str = "", user_id=None) -> dict:
    """一句话 / 一张图 → 一条账单草稿。失败也留日志（调 prompt 时靠它复盘）。"""
    today = datetime.now(TZ).date().isoformat()
    with_image = bool(image)
    model = ""
    draft = None
    usage = {}
    started = time.time()
    status, err = "ok", ""
    try:
        api_key, model = ai_settings()
        if not api_key:
            raise RuntimeError("AI 未配置，请管理员在「设置」里填 API Key")

        prompt = ai_prompt(today, with_image)
        if with_image:
            # 图片消息走 OpenAI 兼容的 image_url + base64，智谱原生支持。
            # 这里**只发补充说明**：prompt 已经在 system 里了，两边都放等于把 900 多字发两遍，
            # 实测白吃 539 个输入 token（2617 → 2078，-21%），延迟和结果都不变。
            user_content = [
                {"type": "text", "text": "用户的补充说明：" + (text or "（无）")},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        else:
            user_content = text

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            # 识图给 600 就够（实测压推理后输出只有 113）。原来给 1200 反而是在鼓励它
            # 「多想一会儿」——线上那次 26 秒、被截断成非法 JSON，就是 1200 被推理吃光。
            "max_tokens": 600 if with_image else 800,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            # 一律压推理。识图曾经「不压」是怕看错金额，2026-10-28 用两张真实账单截图实测证伪：
            #   不压：12.1s，推理 410 token，分类 娱乐/游戏
            #   压了：4.4s，推理 0 token，分类 娱乐/其他，金额四档（1600/1024/800）全对
            # 压图相反没用——1600→800 输入 token 降 43%，耗时只从 4.4 变 4.2，在噪声里。
            # 分类退化成「其他」人点一下胶囊就改了，等 8 秒改不了。
            "reasoning_effort": "low",
        }

        if AI_MOCK:
            data = _ai_mock_reply(text, with_image)
        else:
            try:
                data = _ai_send(body, api_key)
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:200]
                # 视觉请求可能不吃 JSON 模式：去掉再试一次，别让识图整个不可用
                if e.code == 400 and "response_format" in body:
                    body.pop("response_format", None)
                    data = _ai_send(body, api_key)
                else:
                    raise RuntimeError(f"AI 接口错误 {e.code}: {detail}")
            except Exception as e:
                raise RuntimeError(f"AI 请求失败: {e}")

        usage = data.get("usage") or {}
        content = (data["choices"][0]["message"].get("content") or "").strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
        try:
            raw = json.loads(content)
        except Exception:
            raise RuntimeError("AI 返回的内容不是合法 JSON")

        draft = ai_normalize(raw, today, with_image)
        return draft
    except Exception as e:
        status, err = "error", str(e)[:300]
        raise
    finally:
        ai_log_write(
            kind="image" if with_image else "text",
            model=model,
            user_id=user_id,
            brief=(text or "")[:120],
            image_bytes=_data_url_bytes(image) if with_image else 0,
            usage=usage, status=status, error=err, draft=draft,
            latency_ms=int((time.time() - started) * 1000),
        )


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "FamilyLedger/1.0"

    # ---- 工具 ----
    def _json(self, obj, status=200, extra_headers=None):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _csv(self, filename, header, rows):
        """导出 CSV。

        BOM 必须有：不加的话 Excel 打开中文全是乱码，而「打开是乱码」会让人以为文件坏了，
        直接删掉——等于白做。行尾用 CRLF，这是 CSV 的通行写法，Excel 和 WPS 都认。
        """
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(header)
        w.writerows(rows)
        payload = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _export_csv(self):
        """按当前筛选导出 CSV。

        为什么要有：账本现在只能靠备份包才能把数据拿出来——而备份包是给机器看的。
        想用 Excel 算一下、想换系统、想发给对方对账，都需要一份人也能打开的明细。

        过滤条件走 tx_filter，与明细列表**同一段**代码：导出必须和页面上看到的一模一样，
        否则用户勾了「待报销」导出，文件里却混着别的，比没有导出更糟。
        与列表唯一的区别是不截断——列表有 500 行上限（页面翻不动那么多），
        导出是「把筛选结果整份拿走」，截断就成了悄悄少数据。

        金额两列：`金额(元)` 是这笔记录原本的数，`净额(元)` 是**这一行计入净流出的金额**：
        支出 = 原额 − 已分配冲抵；退款/报销 = 已分给支出的部分记 0（那笔钱已经扣在原单上了），
        只剩没分配出去的部分记负数；收入记负数。这样整列 SUM 出来的就是页头的净流出，
        而不是把同一笔退款算两遍。页面行里的大数字仍是「这笔记录自己的净额」
        （退款行显示 ↩ 原额），两者对支出行一致，对已分配完的冲抵行会差一个 0。
        """
        qs = parse_qs(urlparse(self.path).query)
        clause, args, order = tx_filter(qs)
        # 文件名要用到 month，但**只用来拼文件名**，不参与过滤（过滤已由 tx_filter 完成）
        month_tag = (qs.get("month") or [""])[0]
        with db() as conn:
            rows = conn.execute(
                "SELECT t.tx_date, t.direction, t.offset_kind, t.cat_parent, t.category, "
                " t.amount_cents, " + OFFSET_USED_SQL + " AS offset_used, "
                " " + OFFSET_GIVEN_SQL + " AS offset_given, "
                " t.note, t.shared, t.reimb_pending, t.reimbursed, "
                " t.pending_use, t.travel, u.display_name AS owner, "
                " p.tx_date AS pdate, p.cat_parent AS pcat, p.category AS psub "
                "FROM transactions t JOIN users u ON u.id=t.owner_id "
                "LEFT JOIN transactions p ON p.id=t.parent_id "
                "WHERE " + clause +
                " ORDER BY " + order, args).fetchall()

        out = []
        for r in rows:
            if r["direction"] == "refund":
                kind = "退款" if r["offset_kind"] == "refund" else "报销到账"
            else:
                kind = {"expense": "支出", "income": "收入"}.get(r["direction"], r["direction"])
            marks = [n for n, on in (("共享", r["shared"]), ("待报销", r["reimb_pending"]),
                                     ("已报销", r["reimbursed"]), ("待用券", r["pending_use"]),
                                     ("旅行", r["travel"])) if on]
            if r["direction"] == "expense":
                net = r["amount_cents"] - (r["offset_used"] or 0)
            elif r["direction"] == "refund":
                # 已分配出去的部分不能再算一次：它已经从原单的净额里扣掉了
                net = -max(0, r["amount_cents"] - (r["offset_given"] or 0))
            else:
                net = -r["amount_cents"]
            out.append([
                r["tx_date"], kind, r["cat_parent"], r["category"],
                "%.2f" % (r["amount_cents"] / 100.0),      # 不带 ¥，Excel 才好求和
                "%.2f" % (net / 100.0),
                r["owner"], "、".join(marks), r["note"],
                ("%s %s/%s" % (r["pdate"], r["pcat"], r["psub"])) if r["pdate"] else "",
            ])
        # 文件名只认「YYYY-MM」这一种形状，其余一律叫 all：
        # month 是用户传的，白名单比过滤更省心——过滤永远要担心漏了某个字符能把响应头玩坏
        tag = month_tag if re.fullmatch(r"\d{4}-\d{2}", month_tag) else "all"
        return self._csv("ledger-%s.csv" % tag,
                         ["日期", "方向", "一级分类", "子类", "金额(元)", "净额(元)",
                          "归属", "标记", "备注", "关联原单"], out)

    def _err(self, msg, status=400, code=None, extra=None):
        """错误响应。`code` 是给前端做分支用的稳定标识（D-04 起逐步补全），
        前端只需认 code，不必去匹配中文文案——文案随时会改。
        `extra` 用于把「机器可读的明细」一并带回来（如分类改名的预览计划）。"""
        out = {"error": msg}
        if code:
            out["code"] = code
        if extra:
            out.update(extra)
        self._json(out, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_JSON_BODY:
            # 拍小票的请求要带 base64 图片，会到几 MB。超限时给个标记，
            # 免得调用方把「请求太大」误报成「请输入内容」。
            # 注意：必须把 body 读掉再响应——客户端还在写的时候直接返回，
            # 连接会被 RST，调用方看到的是「请求失败」而不是 413。
            left = min(length, 16 * 1024 * 1024)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            return {"_oversize": True}
        try:
            obj = json.loads(self.rfile.read(length).decode())
        except Exception:
            # 记一笔：调用方要知道这是「格式错」，不是「没填」。以前两者都是空体，
            # 用户发个坏 JSON 只会收到「金额不合法」，根本猜不到是自己发错了格式。
            self._bad_body = True
            return {}
        # 非对象（数组 / 字符串 / 数字）一律当空体：下游全是 body.get(...)，
        # 放个 list 进去就是 AttributeError —— 响应还没发就抛异常，客户端拿到的是
        # 「连接被重置」（status 0），且 /api/login 不登录就能触发。
        if not isinstance(obj, dict):
            self._bad_body = True
            return {}
        return obj

    def _cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return None
        m = c.get("sid")
        return m.value if m else None

    def _user(self):
        token = self._cookie_token()
        if not token:
            return None
        with db() as conn:
            row = conn.execute(
                "SELECT u.id,u.username,u.display_name,u.is_admin FROM sessions s "
                "JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>?",
                (token, now_iso()),
            ).fetchone()
        if row:
            self._who = row["username"]      # 只给访问日志用，日志比请求晚一步打印
        return dict(row) if row else None

    def _require(self):
        u = self._user()
        if not u:
            self._err("未登录", 401)
        return u

    def log_message(self, fmt, *args):
        """一行式访问日志：时间 方法 路径 状态 用户 耗时。

        以前这里是 `pass`——4xx/5xx 一个都不留，出了「点保存没反应」只能翻 journal 的
        traceback（而且只有 500 才有 traceback）。全记又会把有用的淹掉：一天几百次 GET。
        折中：写操作（POST/PATCH/DELETE）和 ≥400 一律记，正常浏览不记。
        """
        try:
            code = int(args[1]) if len(args) > 1 else 0
        except Exception:
            code = 0
        if code < 400 and (self.command or "") not in ("POST", "PATCH", "PUT", "DELETE"):
            return
        ms = int((time.time() - getattr(self, "_t0", time.time())) * 1000)
        print("%s %-6s %s %s user=%s %dms" % (
            datetime.now(TZ).strftime("%m-%d %H:%M:%S"), self.command or "-",
            self.path, code, getattr(self, "_who", None) or "-", ms), flush=True)

    # ---- 路由 ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._responded = False
        self._t0 = time.time()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._safe(self._serve_html)
        if path == "/sw.js":
            return self._safe(self._serve_kill_sw)
        if path in STATIC_FILES:
            return self._safe(self._serve_static, *STATIC_FILES[path])
        if path == "/healthz":
            return self._json({"status": "ok"})
        if path.startswith("/api/"):
            return self._safe(self._api_get, path)

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _safe(self, fn, *args):
        """兜底：未预期异常必须变成 500 响应，不能让连接被 RST。

        客户端拿到「连接重置」时无法区分「服务器崩了」和「网络断了」，
        我们也丢掉这次请求的全部上下文。所以先打印现场（进 journalctl），
        再回一个体面的 500；响应已经发出去的话（异常发生在写完响应之后）
        就只能记日志，不能再写第二次。
        """
        try:
            return fn(*args)
        except (BrokenPipeError, ConnectionResetError):
            raise                       # 客户端自己断了，不是我们的问题
        except Exception:
            traceback.print_exc()
            if not getattr(self, "_responded", False):
                try:
                    self._err("服务器开小差了，请稍后再试", 500, code="internal_error")
                except Exception:
                    pass


    def _serve_static(self, rel_path, content_type):
        """图标 / manifest 这类主屏幕资源。

        图标都做成满幅不透明（无 alpha）：iOS 会自己按 squircle 裁圆角，
        留透明边在上屏时会露出黑底。缓存给 7 天——图标改版后想让手机立刻
        换掉，得先删除桌面图标再重新添加，光刷页面没用。
        """
        try:
            data = (BASE_DIR / rel_path).read_bytes()
        except Exception:
            return self._err("文件缺失", 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_kill_sw(self):
        """
        自毁 Service Worker。

        这个域名原先跑过 BeeCount 的 PWA，手机上注册的 SW 会一直拿缓存里的旧壳
        拦截导航，导致换站后仍显示旧页面。浏览器在导航时会重新拉取 /sw.js 做更新
        检查（走网络、不受 SW 缓存影响），所以在这里返回一段「清空缓存 + 注销自己」
        的脚本，任何残留的旧客户端下次访问就会自愈。
        """
        js = (
            "self.addEventListener('install',function(){self.skipWaiting();});\n"
            "self.addEventListener('activate',function(e){e.waitUntil((async function(){\n"
            "  var keys=await caches.keys();\n"
            "  await Promise.all(keys.map(function(k){return caches.delete(k);}));\n"
            "  await self.registration.unregister();\n"
            "  var cs=await self.clients.matchAll({type:'window'});\n"
            "  cs.forEach(function(c){try{c.navigate(c.url);}catch(_){}});\n"
            "})());});\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(js)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(js)


    def do_POST(self):
        self._responded = False
        self._t0 = time.time()
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._err("not found", 404)
        return self._safe(self._api_post, path)

    def do_PATCH(self):
        self._responded = False
        self._t0 = time.time()
        path = urlparse(self.path).path
        return self._safe(self._api_patch, path)

    def do_DELETE(self):
        self._responded = False
        self._t0 = time.time()
        path = urlparse(self.path).path
        return self._safe(self._api_delete, path)

    def _serve_html(self):
        try:
            data = HTML_PATH.read_bytes()
        except Exception:
            return self._err("页面文件缺失", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        # CSP 只加「跟脚本无关」的那几条：页面是单文件内联脚本，一旦写 script-src
        # 就得先算 hash 或改 nonce，而这两条都必须实机验证页面不白屏——手上没有浏览器
        # 自动化，先不赌。下面四条不碰脚本源，纯增益：
        #   frame-ancestors 防被套 iframe（比 X-Frame-Options 更普适，两者并存不冲突）
        #   base-uri 防注入 <base> 把相对地址全搬走
        #   object-src 禁 <object>/<embed>（这页一个都没有）
        #   form-action 禁表单外发（这页没有 form，全是 fetch）
        self.send_header("Content-Security-Policy",
                         "frame-ancestors 'none'; base-uri 'self'; "
                         "object-src 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    # ---- GET ----
    def _api_get(self, path):
        if path == "/api/me":
            u = self._user()
            return self._json({"user": u})

        u = self._require()
        if not u:
            return

        if path == "/api/meta":
            with db() as conn:
                # 只返回真人账号：管理员是后台账号，不该出现在「归属」候选里
                users = [dict(r) for r in conn.execute(
                    "SELECT id, username, display_name FROM users WHERE is_admin=0 ORDER BY id")]
                has_ai = bool(conn.execute(
                    "SELECT 1 FROM settings WHERE key='ai_api_key' AND value<>''").fetchone())
            return self._json({
                "user": u,
                "users": users,
                "categories": TREES,
                "cat_colors": CAT_COLORS,
                "ai_enabled": has_ai,
            })

        if path == "/api/categories/config":
            # 分类配置（管理员）：整棵树 + 色位 + 每个大类在用多少笔，前端管理面板的编辑源
            if not u["is_admin"]:
                return self._err("仅管理员可查看", 403)
            month = datetime.now(TZ).strftime("%Y-%m")
            with db() as conn:
                row = conn.execute("SELECT value FROM settings WHERE key=?",
                                   (CATEGORY_CONFIG_KEY,)).fetchone()
                # 用量只为「删之前知道影响面」和行尾的「本月 N 笔」，不算统计口径：
                # 按 direction + cat_parent 聚合，已删除分类留下的历史账单也会出现在这里（total 大于 0）
                usage = {}
                mb = month_bounds(month)
                for r in conn.execute(
                        "SELECT direction AS d, cat_parent AS p, COUNT(*) AS n, "
                        "SUM(CASE WHEN tx_date>=? AND tx_date<? THEN 1 ELSE 0 END) AS m "
                        "FROM transactions GROUP BY direction, cat_parent", mb):
                    usage.setdefault(r["d"], {})[r["p"]] = {"total": r["n"], "month": r["m"] or 0}
            cfg = json.loads(row["value"]) if row else _default_category_config()
            return self._json({"config": cfg, "usage": usage})

        if path == "/api/categories/logs":
            if not u["is_admin"]:
                return self._err("仅管理员可查看", 403)
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT l.*, u.display_name AS user_name FROM category_log l "
                    "LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 50")]
            return self._json({"items": rows})

        if path == "/api/tx":
            # 过滤条件统一由 tx_filter 编译，导出 CSV 用的是同一段（见 _export_csv）
            clause, args, order = tx_filter(parse_qs(urlparse(self.path).query))
            with db() as conn:
                # 派生金额不再写成每行 6 个相关子查询（500 行 = 3000 次探测）。改成：
                # 先查主列表，再用两条 GROUP BY 把这一页涉及的原单/冲抵一次算完，在内存里拼回去。
                # 语义与子查询逐字对齐，见下方 agg_*；改这里必须同时改 _api_get 的冲抵明细。
                rows = [dict(r) for r in conn.execute(
                    "SELECT t.*, u.display_name AS owner_name, c.display_name AS creator_name, "
                    "p.tx_date AS parent_date, p.cat_parent AS parent_cat_parent, "
                    "p.category AS parent_category, p.amount_cents AS parent_amount "
                    "FROM transactions t JOIN users u ON u.id=t.owner_id "
                    "JOIN users c ON c.id=t.created_by "
                    "LEFT JOIN transactions p ON p.id=t.parent_id WHERE " + clause +
                    " ORDER BY " + order + " LIMIT 500", args)]
                ids = [r["id"] for r in rows]
                by_parent, by_offset = {}, {}
                if ids:
                    marks = ",".join("?" * len(ids))
                    # 作为原单：已退（只算退货）/已报（只算报销）/到账日期（只算报销）/冲抵笔数
                    for r in conn.execute(
                            "SELECT l.parent_id AS pid, "
                            "COALESCE(SUM(CASE WHEN r.offset_kind='refund' THEN l.amount_cents END),0) AS refunded, "
                            "COALESCE(SUM(CASE WHEN r.offset_kind='reimb' THEN l.amount_cents END),0) AS reimb, "
                            "MAX(CASE WHEN r.offset_kind='reimb' THEN r.tx_date END) AS reimb_date, "
                            "COUNT(DISTINCT l.offset_id) AS n "
                            "FROM offset_links l JOIN transactions r ON r.id=l.offset_id "
                            "WHERE l.parent_id IN (" + marks + ") GROUP BY l.parent_id", ids):
                        by_parent[r["pid"]] = r
                    # 作为冲抵：分给了几笔、分出去多少（差额就是「未分配」）
                    for r in conn.execute(
                            "SELECT offset_id AS oid, COUNT(*) AS n, "
                            "COALESCE(SUM(amount_cents),0) AS total FROM offset_links "
                            "WHERE offset_id IN (" + marks + ") GROUP BY offset_id", ids):
                        by_offset[r["oid"]] = r
                for r in rows:
                    bp = by_parent.get(r["id"])
                    bo = by_offset.get(r["id"])
                    # 没有记录时：SUM 补 0、MAX 留 None、COUNT 补 0 —— 与 COALESCE 子查询一致
                    r["refunded_cents"] = bp["refunded"] if bp else 0
                    r["reimb_cents"] = bp["reimb"] if bp else 0
                    r["reimb_date"] = bp["reimb_date"] if bp else None
                    r["refund_count"] = bp["n"] if bp else 0
                    r["link_count"] = bo["n"] if bo else 0
                    r["link_total"] = bo["total"] if bo else 0
                agg = conn.execute(
                    "SELECT COUNT(*) cnt, "
                    "SUM(CASE WHEN t.direction='expense' THEN t.amount_cents ELSE 0 END) expense, "
                    "SUM(CASE WHEN t.direction='income' THEN t.amount_cents ELSE 0 END) income, "
                    "SUM(CASE WHEN t.direction='refund' AND t.offset_kind='refund' "
                    "     THEN t.amount_cents ELSE 0 END) refund, "
                    "SUM(CASE WHEN t.direction='refund' AND t.offset_kind='reimb' "
                    "     THEN t.amount_cents ELSE 0 END) reimb "
                    "FROM transactions t WHERE " + clause, args).fetchone()
            for r in rows:
                # 剩余可冲抵 = 原额 − 退货 − 报销；而「已退」徽标只看退货那一半
                r["remain_cents"] = max(
                    0, r["amount_cents"] - (r["refunded_cents"] or 0) - (r["reimb_cents"] or 0))
                # 幂等键是服务端内部状态，不该跟着列表吐给前端
                r.pop("client_token", None)
            return self._json({
                "items": rows,
                "summary": {
                    "count": agg["cnt"] or 0,
                    "expense": agg["expense"] or 0,
                    "income": agg["income"] or 0,
                    "refund": agg["refund"] or 0,
                    "reimb": agg["reimb"] or 0,
                },
            })

        if path == "/api/refund-targets":
            # 手动记退款时找「能冲抵哪一笔」。三条通道，按匹配强度排序：
            #   same 金额完全相等 | kw 备注关键词命中（部分退款）| near 金额在容差带内
            qs = parse_qs(urlparse(self.path).query)

            def one(k):
                return (qs.get(k) or [""])[0]

            try:
                amount = int(one("amount_cents") or 0)
            except ValueError:
                amount = 0
            if amount < 0:
                amount = 0
            owner = one("owner")
            note = one("note").strip()
            # kind=reimb 时报销复用这套匹配：找「还没报过的支出」，已经标成已报销的别再出现
            if one("kind") == "reimb":
                # 报销候选和退款候选必须分开，三条原因：
                #   1) 报销是「一次到账摊多笔」，单笔金额天生小于到账额；沿用它那套
                #      「单笔剩余得 ≥ 到账金额」的规则，真正该报的支出一笔都出不来
                #      （2026-10-28 实测：填 5000，5 笔待报销全被丢弃，反而推上来一笔没标记的 7533.90）
                #   2) 只认你亲手标过「待报销」的支出，否则近 30 天任何有余额的消费都会进候选
                #   3) 差旅报销常跨月，30 天窗口对报销太短
                try:
                    owner_id = int(owner) if owner else None
                except ValueError:
                    owner_id = None
                return self._reimb_candidates(owner_id)

            since = (datetime.now(TZ) - timedelta(days=REFUND_WINDOW_DAYS)).date().isoformat()

            def fetch(with_owner):
                sql = ("SELECT t.id, t.tx_date, t.cat_parent, t.category, t.note, "
                       "t.amount_cents, t.owner_id, "
                       "(SELECT COALESCE(SUM(l.amount_cents),0) FROM offset_links l "
                       " WHERE l.parent_id=t.id) AS offset_cents "
                       "FROM transactions t WHERE t.direction='expense' AND t.tx_date>=?")
                args = [since]
                if with_owner is not None:
                    sql += " AND t.owner_id=?"
                    args.append(with_owner)
                # 窗口只有 30 天（约 200 条），不再靠 LIMIT 截断——旧版「先取 40 条再剔退完的」
                # 会把真正该冲抵的那笔挡在门外
                sql += " ORDER BY t.tx_date DESC, t.id DESC LIMIT 500"
                with db() as conn:
                    return [dict(r) for r in conn.execute(sql, args)]

            try:
                owner_id = int(owner) if owner else None
            except ValueError:
                owner_id = None
            rows = fetch(owner_id)
            relaxed = False

            # 容差：退款金额一定 <= 可退余额（POST /api/tx 会拒），所以只向上放宽，
            # 覆盖「一单多件只退其中一件」的部分退款
            tol = min(max(REFUND_TOL_FLOOR, amount // 2), REFUND_TOL_CAP) if amount else 0
            kws = refund_keywords(note)
            items = []
            for r in rows:
                remain = r["amount_cents"] - (r["offset_cents"] or 0)
                if remain <= 0:
                    continue
                if amount and remain < amount:
                    continue        # 退不了的候选不给，点了必然报「最多还能退 X」
                diff = remain - amount
                hit_kw = any(k in (r["note"] or "") for k in kws)
                if amount:
                    if diff == 0:
                        kind = "same"
                    elif hit_kw:
                        kind = "kw"
                    elif diff <= tol:
                        kind = "near"
                    else:
                        continue
                else:
                    kind = "kw" if hit_kw else "near"
                r.update(remain_cents=remain, diff_cents=diff, match=kind)
                items.append(r)
            # 稳定排序两次：先日期近的在前，再按「匹配强度 → 金额差」排
            rank = {"same": 0, "kw": 1, "near": 2}
            items.sort(key=lambda r: (r["tx_date"], r["id"]), reverse=True)
            items.sort(key=lambda r: (rank[r["match"]], abs(r["diff_cents"])))

            # 预选最强候选；但 top1/top2 完全同分（同额两笔、同日同额）时不预选，
            # 那种情况猜错概率太高，交给用户自己点
            auto = None
            if amount and items:
                top = items[0]
                tied = (len(items) > 1
                        and rank[items[1]["match"]] == rank[top["match"]]
                        and abs(items[1]["diff_cents"]) == abs(top["diff_cents"]))
                if not tied:
                    auto = top["id"]
            return self._json({
                "items": items[:REFUND_TARGET_LIMIT],
                "auto": auto,
                "window_days": REFUND_WINDOW_DAYS,
                "tolerance_cents": tol,
                "relaxed": relaxed,      # True = 该归属人下没有候选，已放宽到全部人
            })

        # 一条冲抵的分配明细：分给了哪几笔支出、各多少、还剩多少没分配
        m = re.fullmatch(r"/api/tx/(\d+)/links", path)
        if m:
            return self._get_links(int(m.group(1)))

        if path == "/api/refunds":
            parent = (parse_qs(urlparse(self.path).query).get("parent") or [""])[0]
            if not parent:
                return self._err("缺少 parent 参数")
            try:
                pid = int(parent)
            except ValueError:
                return self._err("parent 参数非法")
            with db() as conn:
                # owner_name = 这笔钱算谁的（继承原单）；created_name = 谁动手退的
                # amount_cents 是**分配**给这笔原单的金额（一条冲抵可能摊在多笔上）
                rows = [dict(r) for r in conn.execute(
                    "SELECT r.id, r.tx_date, l.amount_cents, r.offset_kind, r.note, "
                    "u.display_name AS owner_name, c.display_name AS created_name "
                    "FROM offset_links l JOIN transactions r ON r.id=l.offset_id "
                    "JOIN users u ON u.id=r.owner_id "
                    "JOIN users c ON c.id=r.created_by "
                    "WHERE l.parent_id=? ORDER BY r.tx_date, r.id", (pid,))]
            return self._json({"items": rows})

        if path == "/api/export.csv":
            return self._export_csv()

        if path == "/api/stats":
            q = parse_qs(urlparse(self.path).query)
            month = (q.get("month") or [datetime.now(TZ).strftime("%Y-%m")])[0]
            # 闭开区间代替 LIKE：让 tx_date 走索引（S-01）。month 传空/写错时退化成
            # 一对不可能命中的边界，统计全 0——与旧的 LIKE '-%' 行为一致，不会吐出全表。
            mb = month_bounds(month) or ("0000-00-00", "0000-00-00")
            with db() as conn:
                gross = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) FROM transactions "
                    "WHERE direction='expense' AND tx_date>=? AND tx_date<?", mb).fetchone()[0]

                # 冲抵分两类：'refund' 退货退款 / 'reimb' 报销到账。统计上都是负支出（都进 net_expense），
                # 但报表要各说各的——「退款」卡里混进报销，数字就没法解释了。
                def off_parent(kind):
                    # 分配回本月支出的冲抵，不管冲抵发生在哪个月（归属月口径）
                    sql = ("SELECT COALESCE(SUM(l.amount_cents),0) FROM offset_links l "
                           "JOIN transactions r ON r.id=l.offset_id AND r.direction='refund' "
                           "JOIN transactions p ON p.id=l.parent_id AND p.direction='expense' "
                           "WHERE p.tx_date>=? AND p.tx_date<?")
                    args = list(mb)
                    if kind:
                        sql += " AND r.offset_kind=?"
                        args.append(kind)
                    return conn.execute(sql, args).fetchone()[0]

                def off_orphan(kind):
                    # 没分配出去的部分（含完全无主的冲抵）只能算在它发生的月份。
                    # 部分分配也走这里：到账 900 只认领了 700，那 200 先当无主，以后认领了再挪。
                    sql = ("SELECT COALESCE(SUM(r.amount_cents - COALESCE("
                           "(SELECT SUM(l.amount_cents) FROM offset_links l "
                           " WHERE l.offset_id=r.id),0)),0) "
                           "FROM transactions r WHERE r.direction='refund' "
                           "AND r.tx_date>=? AND r.tx_date<?")
                    args = list(mb)
                    if kind:
                        sql += " AND r.offset_kind=?"
                        args.append(kind)
                    return conn.execute(sql, args).fetchone()[0]

                offset = off_parent("")                 # 两类合计，用于净支出
                orphan = off_orphan("")
                offset_ref = off_parent("refund")
                offset_rei = off_parent("reimb")
                orphan_ref = off_orphan("refund")
                orphan_rei = off_orphan("reimb")
                income = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) FROM transactions "
                    "WHERE direction='income' AND tx_date>=? AND tx_date<?", mb).fetchone()[0]
                by_cat = [dict(r) for r in conn.execute(
                    "SELECT 'expense' AS direction, cat_parent, SUM(net) total FROM ("
                    "  SELECT cat_parent, amount_cents net FROM transactions"
                    "   WHERE direction='expense' AND tx_date>=? AND tx_date<?"
                    "  UNION ALL"
                    # 跨分类的一次报销要按分配逐笔抵扣：900 拆给「其他支出 700」和「交通 200」，
                    # 减的就是各自原单的分类，不是冲抵记录自己的分类
                    "  SELECT p.cat_parent, -l.amount_cents FROM offset_links l"
                    "   JOIN transactions r ON r.id=l.offset_id AND r.direction='refund'"
                    "   JOIN transactions p ON p.id=l.parent_id AND p.direction='expense'"
                    "   WHERE p.tx_date>=? AND p.tx_date<?"
                    ") GROUP BY cat_parent ORDER BY total DESC",
                    (mb[0], mb[1], mb[0], mb[1]))]
                # 饼图只画正数分类，负数分类会被 HAVING 悄悄丢掉，于是「各项加起来」比
                # 「支出」多出无主冲抵那部分，看图的人自己怎么加都对不上。把两个差额
                # 显式给出来，前端就能摆一道真能对上的算式：
                #   分类合计 − 冲抵后为负的 − 无主冲抵 = 净支出
                by_cat_pos = [c for c in by_cat if c["total"] > 0]
                cat_negative = -sum(c["total"] for c in by_cat if c["total"] < 0)
                by_cat = by_cat_pos
                by_owner = [dict(r) for r in conn.execute(
                    "SELECT u.display_name name, u.id, SUM(x.amt) expense FROM ("
                    "  SELECT owner_id, amount_cents amt FROM transactions"
                    "   WHERE direction='expense' AND tx_date>=? AND tx_date<?"
                    "  UNION ALL"
                    "  SELECT p.owner_id, -l.amount_cents FROM offset_links l"
                    "   JOIN transactions r ON r.id=l.offset_id AND r.direction='refund'"
                    "   JOIN transactions p ON p.id=l.parent_id AND p.direction='expense'"
                    "   WHERE p.tx_date>=? AND p.tx_date<?"
                    ") x JOIN users u ON u.id=x.owner_id GROUP BY u.id HAVING SUM(x.amt) > 0",
                    (mb[0], mb[1], mb[0], mb[1]))]
                shared = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) FROM transactions "
                    "WHERE direction='expense' AND shared=1 AND tx_date>=? AND tx_date<?",
                    mb).fetchone()[0] - conn.execute(
                    "SELECT COALESCE(SUM(l.amount_cents),0) FROM offset_links l "
                    "JOIN transactions r ON r.id=l.offset_id AND r.direction='refund' "
                    "JOIN transactions p ON p.id=l.parent_id "
                    "WHERE p.direction='expense' AND p.shared=1 AND p.tx_date>=? AND p.tx_date<?",
                    mb).fetchone()[0]
                # 待报销、待用券都按“跨月累计 + 净值”，退掉的钱不该还算在垫付里
                def net_rows(where_sql, where_args=()):
                    return [r["net"] for r in conn.execute(
                        "SELECT t.amount_cents - COALESCE((SELECT SUM(l.amount_cents) "
                        "FROM offset_links l WHERE l.parent_id=t.id),0) AS net "
                        "FROM transactions t WHERE " + where_sql, where_args)]
                px = [v for v in net_rows("t.reimb_pending=1") if v > 0]
                vx = [v for v in net_rows("t.pending_use=1") if v > 0]
            net_expense = gross - offset - orphan
            return self._json({
                "month": month,
                "expense": net_expense,          # 净支出（退款和报销都已冲抵）
                "expense_gross": gross,
                "refund": offset_ref + orphan_ref,       # 只算退货退款，不含报销
                "reimb": offset_rei + orphan_rei,        # 报销到账（真收到的钱）
                "refund_offset": offset_ref,     # 冲抵回本月的金额
                "refund_orphan": orphan_ref,     # 本月发生的无主退款
                "reimb_offset": offset_rei,
                "reimb_orphan": orphan_rei,
                "income": income,
                "shared": max(0, shared),
                "pending": sum(px),
                "pending_cnt": len(px),
                "voucher": sum(vx),
                "voucher_cnt": len(vx),
                "by_category": by_cat,
                "cat_negative": cat_negative,    # 冲抵后为负、未进饼图的分类合计（正数）
                "cat_total": sum(c["total"] for c in by_cat),   # 饼图各项之和
                "by_owner": by_owner,
            })

        if path == "/api/settings":
            if not u["is_admin"]:
                return self._err("仅管理员可查看", 403)
            key, model = ai_settings()
            return self._json({
                "ai_model": model,
                "ai_key_masked": (key[:6] + "…" + key[-4:]) if key else "",
                "ai_enabled": bool(key),
            })

        if path == "/api/ai/logs":
            if not u["is_admin"]:
                return self._err("仅管理员可查看", 403)
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = int((qs.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            limit = min(100, max(1, limit))
            since = (datetime.now(TZ) - timedelta(days=7)).isoformat(timespec="seconds")
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM ai_log ORDER BY id DESC LIMIT ?", (limit,))]
                agg = conn.execute(
                    "SELECT COUNT(*) n, SUM(status='ok') ok, SUM(prompt_tokens) pt, "
                    "SUM(completion_tokens) ct, SUM(latency_ms) ms "
                    "FROM ai_log WHERE ts>=?", (since,)).fetchone()
            for r in rows:
                try:
                    r["draft"] = json.loads(r["draft"]) if r["draft"] else None
                except Exception:
                    r["draft"] = None
            n = agg["n"] or 0
            return self._json({
                "items": rows,
                "week": {
                    "count": n,
                    "ok": agg["ok"] or 0,
                    "prompt_tokens": agg["pt"] or 0,
                    "completion_tokens": agg["ct"] or 0,
                    "avg_ms": int((agg["ms"] or 0) / n) if n else 0,
                },
            })

        if path == "/api/loans":
            # 借款台账：独立于 transactions，不参与任何统计，只是个「别忘了」的提醒
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT l.*, u.display_name AS owner_name FROM loans l "
                    "JOIN users u ON u.id=l.owner_id "
                    "ORDER BY (l.settled_at IS NOT NULL), l.loan_date DESC, l.id DESC")]
            out_total = in_total = 0
            for r in rows:
                r["remain_cents"] = max(0, r["amount_cents"] - (r["repaid_cents"] or 0))
                r["settled"] = bool(r["settled_at"]) or r["remain_cents"] <= 0
                if not r["settled"]:
                    if r["direction"] == "out":
                        out_total += r["remain_cents"]
                    else:
                        in_total += r["remain_cents"]
            return self._json({
                "items": rows,
                "summary": {"out_open": out_total, "in_open": in_total,
                            "open_count": sum(1 for r in rows if not r["settled"])},
            })

        return self._err("not found", 404)

    # ---- POST ----
    def _api_post(self, path):
        body = self._body()

        # 调试开关：只为复现「连上了但不回数据」这一类故障，默认关闭。
        #   LEDGER_DEBUG_HANG=1 python3 server.py
        # 生产环境不要设这个变量。ThreadingHTTPServer 下睡的是本请求自己的线程，
        # 不会把别的请求一起堵住，所以可以放心用它测前端的超时与按钮复位。
        if os.environ.get("LEDGER_DEBUG_HANG") and path == "/api/tx":
            time.sleep(30)

        if path == "/api/login":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            ip = self.client_address[0]
            if too_many_attempts(ip, username):
                return self._err("尝试次数过多，请 10 分钟后再试", 429)
            with db() as conn:
                row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                if not row or not verify_password(password, row["pwd_hash"]):
                    note_attempt(ip, username, False)
                    return self._err("用户名或密码不正确", 401)
                note_attempt(ip, username, True)
                cleanup_sessions(conn)           # 顺手清过期会话，节流到 6 小时一次
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(TZ) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (token, row["id"], expires, now_iso()))
            cookie = f"sid={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_DAYS*86400}"
            return self._json({"ok": True}, extra_headers=[("Set-Cookie", cookie)])

        if path == "/api/logout":
            token = self._cookie_token()
            if token:
                with db() as conn:
                    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            return self._json({"ok": True},
                              extra_headers=[("Set-Cookie", "sid=; Path=/; Max-Age=0; SameSite=Lax")])

        u = self._require()
        if not u:
            return

        if path == "/api/password":
            old = body.get("old") or ""
            new = body.get("new") or ""
            if len(new) < 8:
                return self._err("新密码至少 8 位")
            with db() as conn:
                row = conn.execute("SELECT pwd_hash FROM users WHERE id=?", (u["id"],)).fetchone()
                if not verify_password(old, row["pwd_hash"]):
                    return self._err("原密码不正确", 403)
                conn.execute("UPDATE users SET pwd_hash=? WHERE id=?",
                             (hash_password(new), u["id"]))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
            return self._json({"ok": True})

        if path == "/api/tx":
            return self._create_tx(u, body)

        # 报销到账：生成一条挂原单的冲抵记录（offset_kind='reimb'），同时把原单标成已报销。
        # 和明细里点「退」走同一条冲抵路径，区别只在记录的 kind 和这条不做分类校验。
        m = re.fullmatch(r"/api/tx/(\d+)/reimburse", path)
        if m:
            return self._reimburse(int(m.group(1)), u, body)

        # 冲抵分配：一次到账可以摊在多笔支出上。整体替换（不是逐条增删），
        # 这样一次请求内金额守恒成立，不会出现「删了旧的还没加新的」的中间态。
        m = re.fullmatch(r"/api/tx/(\d+)/links", path)
        if m:
            return self._save_links(int(m.group(1)), u, body)

        if path == "/api/ai/parse":
            if body.get("_oversize"):
                return self._err("图片太大，请换一张或降低画质", 413)
            # 频控：按 ai_log 现成的记录算（成功失败都算，都是花了钱的），不另建计数表
            since = (datetime.now(TZ) - timedelta(hours=1)).isoformat(timespec="seconds")
            with db() as conn:
                used = conn.execute(
                    "SELECT COUNT(*) FROM ai_log WHERE user_id=? AND ts>=?",
                    (u["id"], since)).fetchone()[0]
            if used >= AI_QUOTA_PER_HOUR:
                return self._err("这一小时问得太多了（上限 %d 次），过会儿再试" % AI_QUOTA_PER_HOUR,
                                 429, code="ai_quota")
            text = (body.get("text") or "").strip()
            if len(text) > 300:
                return self._err("文字太长，请精简到 300 字以内")
            raw_image = body.get("image") or ""
            if not text and not raw_image:
                return self._err("写一句话，或者拍一张小票")
            try:
                image = ai_check_image(raw_image)
            except ValueError as e:
                return self._err(str(e), 400)
            try:
                return self._json({"draft": ai_parse(text, image, u["id"])})
            except Exception as e:
                return self._err(str(e), 502)

        if path == "/api/users":
            if not u["is_admin"]:
                return self._err("仅管理员可添加账号", 403)
            username = (body.get("username") or "").strip().lower()
            display = (body.get("display_name") or username).strip()
            pwd = body.get("password") or ""
            if not re.fullmatch(r"[a-z0-9_]{3,20}", username):
                return self._err("用户名只能用小写字母、数字、下划线，3-20 位")
            if len(pwd) < 8:
                return self._err("密码至少 8 位")
            with db() as conn:
                if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                    return self._err("用户名已存在")
                conn.execute(
                    "INSERT INTO users(username,display_name,pwd_hash,is_admin,created_at) "
                    "VALUES(?,?,?,0,?)", (username, display, hash_password(pwd), now_iso()))
            return self._json({"ok": True})

        if path == "/api/settings":
            if not u["is_admin"]:
                return self._err("仅管理员可修改", 403)
            with db() as conn:
                if body.get("ai_api_key"):
                    conn.execute("INSERT INTO settings(key,value) VALUES('ai_api_key',?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (body["ai_api_key"].strip(),))
                if body.get("ai_model"):
                    conn.execute("INSERT INTO settings(key,value) VALUES('ai_model',?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (body["ai_model"].strip(),))
            return self._json({"ok": True})

        if path == "/api/categories/config":
            return self._save_category_config(u, body)

        if path == "/api/loans":
            u = self._require()
            if not u:
                return
            try:
                direction = body.get("direction") or "out"
                if direction not in ("out", "in"):
                    raise ValueError("借款方向只能是 out 或 in")
                counterparty = (body.get("counterparty") or "").strip()
                if not counterparty:
                    raise ValueError("对方不能为空")
                cents = int(body.get("amount_cents"))
                if cents <= 0 or cents > 100_000_000:
                    raise ValueError("金额不合法")
                loan_date = parse_tx_date(body.get("loan_date"),
                                          default=datetime.now(TZ).date().isoformat())
                owner_id = int(body.get("owner_id") or u["id"])
            except ValueError as e:          # 自己抛的中文提示，直接给人看
                return self._err(str(e), code=getattr(e, "code", "bad_body"))
            except Exception:
                # int(None) 这类异常的原文是给开发者看的，不进响应体（D-04）
                return self._err("参数不合法，请检查后重试", code="bad_body")
            with db() as conn:
                if not conn.execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone():
                    return self._err("归属用户不存在")
                cur = conn.execute(
                    "INSERT INTO loans(direction,counterparty,amount_cents,repaid_cents,"
                    "loan_date,note,owner_id,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,0,?,?,?,?,?,?)",
                    (direction, counterparty[:20], cents, loan_date,
                     (body.get("note") or "")[:100], owner_id, u["id"], now_iso(), now_iso()))
                new_id = cur.lastrowid
            return self._json({"ok": True, "id": new_id})

        return self._err("not found", 404)

    def _save_category_config(self, u, body):
        """保存分类配置（前端整树提交，整体替换）。

        规则（2026-10-28 定稿，2026-10-30 加固）：
          * 校验下限：每个方向 ≥1 个一级分类、每个一级 ≥1 个二级分类
          * 双重查重：配置内名字唯一；新增名不能撞历史账单里的旧名字
            （历史账单是文本引用，撞名会把两批不相干的账强行合并）
          * 改名 = 全链条 UPDATE：一级按「子类重合度」配对，子类按「同父级下
            消失/新增恰好各一个」配对，配上的在同事务里 UPDATE 历史账单
          * **删除有历史账单的分类一律拦下**（Q-01）：只能改名，不能删
          * **会改写历史的保存必须先预览再确认**：preview 只看不算，confirm 才写
          * 配置 + 改名 UPDATE + 日志必须同一事务；响应在事务外发
        """
        if not u["is_admin"]:
            return self._err("仅管理员可修改分类", 403)
        clean, err = _validate_category_config(body.get("config"))
        if err:
            return self._err(err)
        preview = bool(body.get("preview"))
        confirm = bool(body.get("confirm"))
        with db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (CATEGORY_CONFIG_KEY,)).fetchone()
            old_cfg = json.loads(row["value"]) if row else {}

            # ---- 查重 ----
            # 配置内唯一由 _validate_category_config 保证（子类查重 + 一级 dict 键天然唯一），
            # 这里补一层防御：同方向一级名跨父级重复直接拒绝
            for kind in ("expense", "income", "refund"):
                names = list(clean[kind].keys())
                if len(names) != len(set(names)):
                    return self._err(f"{kind} 里一级分类名重复")
            # 新增名不能撞历史账单：一级和子类分开查
            for kind in ("expense", "income", "refund"):
                for parent, node in clean[kind].items():
                    old_node = old_cfg.get(kind, {}).get(parent)
                    if old_node is None:
                        # 新增一级：历史账单里已经挂着这个名字 → 拒绝
                        n = conn.execute(
                            "SELECT COUNT(*) FROM transactions WHERE direction=? AND cat_parent=?",
                            (kind, parent)).fetchone()[0]
                        if n:
                            return self._err(
                                f"「{parent}」在历史账单已有 {n} 笔记录，"
                                "不能作为新分类名（会被合并到一起）")
                        continue
                    # 一级已存在：新增子类名不能撞历史账单
                    for sub in node["subs"]:
                        if sub in old_node.get("subs", []):
                            continue
                        n = conn.execute(
                            "SELECT COUNT(*) FROM transactions "
                            "WHERE direction=? AND cat_parent=? AND category=?",
                            (kind, parent, sub)).fetchone()[0]
                        if n:
                            return self._err(
                                f"「{parent} / {sub}」在历史账单已有 {n} 笔，不能复用这个名字")

            # ---- 先算计划，一个字都还没写 ----
            plan = _plan_category_change(conn, old_cfg, clean)

            if preview:
                # 只看不算：不写配置、不写日志、不改历史。
                # 放在「有历史就拦」之前——预览的用途就是把「哪些会被拦」先给人看，
                # 它自己不该因为同一个原因变成错误。
                return self._json({"ok": True, "preview": plan})
            if plan["blocked"]:
                return self._err(_blocked_msg(plan), code="category_has_history",
                                 extra={"preview": plan})
            if plan["affected_total"] and not confirm:
                return self._err(
                    "这次保存会改写 %d 笔历史账单，确认后再提交" % plan["affected_total"],
                    code="need_confirm", extra={"preview": plan})

            _apply_category_plan(conn, plan, u)

            # ---- 写配置 + 刷新全局 + 日志（同一事务）----
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (CATEGORY_CONFIG_KEY, json.dumps(clean, ensure_ascii=False)))
            _load_category_config(conn)
            _category_log(conn, u["id"], "update", "", "", "",
                          "保存分类配置" + (f"，改名连带更新 {plan['affected_total']} 笔账单"
                                           if plan["affected_total"] else ""))
        return self._json({"ok": True, "renamed_tx": plan["affected_total"]})

    def _find_by_token(self, token):
        """按幂等键查已经落过账的那条。token 是客户端生成的随机串，跨用户碰撞可忽略不计。"""
        with db() as conn:
            row = conn.execute(
                "SELECT id FROM transactions WHERE client_token=?", (token,)).fetchone()
        return row["id"] if row else None

    def _dup_result(self, token, extra=None):
        """重复提交的响应：形状与首次成功一致，只是多一个 duplicate 标记，
        前端照样走「成功」分支（清空表单、提示已记），不用为幂等写第二套逻辑。"""
        out = {"ok": True, "id": self._find_by_token(token), "duplicate": True,
               "cleared": False}
        if extra:
            out.update(extra)
        return self._json(out)

    def _create_tx(self, u, body):
        # 请求体都没解析成对象，后面的「金额不合法」全是误导：先说清是格式问题
        if getattr(self, "_bad_body", False):
            return self._err("请求体不是合法的 JSON 对象", code="bad_body")
        # 幂等键：客户端每次「提交一笔记账」带一个随机 token，成功后换新的。
        # 它标识的是提交动作而非业务内容，所以两笔真实同额消费互不影响。
        token = str(body.get("client_token") or "").strip()[:64] or None
        try:
            tx_date = parse_tx_date(body.get("tx_date"),
                                    default=datetime.now(TZ).date().isoformat())
        except ValueError as e:
            return self._err(str(e), code=getattr(e, "code", "date_invalid"))
        try:
            cents = parse_cents(body.get("amount_cents"))
        except ValueError as e:
            return self._err(str(e), code=getattr(e, "code", "amount_invalid"))
        try:
            direction = body.get("direction")
            if direction not in TREES:
                raise ValueError("收支方向非法")
            parent = body.get("cat_parent")
            cat = body.get("category")
            parent_id = parse_opt_int(body.get("parent_id"), "原账单")
            owner_id = int(body.get("owner_id") or u["id"])
            # 报销一次到账摊多笔 + 其中一部分是差补：走 links / subsidy 这条路
            links_in = []
            for it in (body.get("links") or []):
                try:
                    pid = int(it.get("parent_id") or 0)
                    c = int(it.get("amount_cents") or 0)
                except Exception:
                    raise ValueError("分配明细格式不对")
                if pid > 0 and c > 0:
                    links_in.append((pid, c))
            subsidy_cents = int(body.get("subsidy_cents") or 0)
            subsidy_cat = (body.get("subsidy_category") or "").strip()
        except ValueError as e:              # 「分配明细格式不对」这类是我们自己写的提示
            return self._err(str(e), code=getattr(e, "code", "bad_body"))
        except Exception:
            return self._err("参数不合法，请检查后重试", code="bad_body")

        # 幂等：同一个 token 已经落过账就直接返回那一条，不再插第二次。
        # 放在参数校验之后——参数不合法时不该消耗掉这个 token，用户改完金额要能用同一个重试。
        if token:
            dup = self._find_by_token(token)
            if dup:
                return self._dup_result(token)

        # 冲抵分两种：退货退款 / 报销到账。界面把这两件事放在同一个方向（「退款/报销」）里填，
        # 记账时由用户选哪一种，数据上始终分开——统计各说各的，明细也能看出这条冲的是货还是款。
        offset_kind = "reimb" if body.get("offset_kind") == "reimb" else "refund"
        if direction != "refund":
            offset_kind = "refund"      # 只有冲抵行才有类型，普通收支一律按默认值存

        # 报销到账的「一次到账摊多笔 + 差补」走独立一条路：一条 POST 里同时落
        # 「冲抵记录」和「差补收入」两条账，避免出现只记了冲抵、差补丢了的中间态。
        if direction == "refund" and offset_kind == "reimb" and (links_in or subsidy_cents):
            try:
                with db() as conn:
                    refund_id, income_id = apply_reimb(
                        conn, u, tx_date, cents, links_in, subsidy_cents,
                        subsidy_cat, owner_id, (body.get("note") or ""), token)
            except ValueError as e:
                return self._err(str(e))
            except sqlite3.IntegrityError:
                # 并发同 token：另一路抢在前头落了账，唯一索引把这条挡下来，当作重复提交
                if token and self._find_by_token(token):
                    return self._dup_result(token)
                raise
            return self._json({"ok": True, "id": refund_id, "income_id": income_id})

        try:
            with db() as conn:
                if not conn.execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone():
                    return self._err("归属用户不存在")

                cleared = False
                if parent_id:
                    # 冲抵原单：金额不能超过剩余可退，分类与归属人继承原单
                    if direction != "refund":
                        return self._err("只有退款可以冲抵原账单")
                    orig = conn.execute(
                        "SELECT * FROM transactions WHERE id=?", (parent_id,)).fetchone()
                    if not orig:
                        return self._err("原账单不存在")
                    if orig["direction"] != "expense":
                        return self._err("只能冲抵支出记录")
                    used = offset_used(conn, parent_id)
                    remain = orig["amount_cents"] - used
                    if remain <= 0:
                        return self._err("这笔已经全部退完了")
                    if cents > remain:
                        return self._err(f"最多还能退 {remain / 100:.2f} 元")
                    parent = orig["cat_parent"]
                    cat = orig["category"]
                    # 归属也跟着原单走：冲抵记录是原单的影子，不该有「谁填的就算谁的」这种独立属性。
                    # 否则给老婆垫的钱报销，会算成你冲减了别人的账。
                    owner_id = orig["owner_id"]
                else:
                    if parent not in TREES[direction]:
                        return self._err("分类不在允许范围内", code="category_invalid")
                    if cat not in TREES[direction][parent]:
                        return self._err("子分类不在允许范围内", code="category_invalid")

                shared = 1 if body.get("shared") else 0
                pending = 1 if body.get("reimb_pending") else 0
                reimbursed = 1 if body.get("reimbursed") else 0
                if pending and reimbursed:      # 互斥：待报销优先，避免出现「既待报销又已报销」
                    reimbursed = 0
                # 「已报销」不是能直接填的字段：它代表这笔钱确实收回来了，得有到账记录、
                # 金额也得跟着减。允许手填只会让净支出虚高而标记显示已报（R14）。
                if reimbursed and direction == "expense":
                    return self._err(
                        "「已报销」不能手动标记：它表示这笔钱已经收回来。"
                        "请记一笔「退款 / 报销」到账，系统会自动标记并冲减支出。",
                        code="reimburse_requires_record")
                if direction != "expense":
                    # 5 个标记只属于支出，一律清空。曾经漏了 reimbursed：
                    # 收入能带着「已报销」，mark=reimb 就筛得出工资（R6）。
                    shared = pending = reimbursed = 0
                    pending_use = travel = 0
                else:
                    pending_use = 1 if body.get("pending_use") else 0
                    travel = 1 if body.get("travel") else 0
                cur = conn.execute(
                    "INSERT INTO transactions(tx_date,direction,cat_parent,category,amount_cents,"
                    "shared,reimbursed,reimb_pending,pending_use,travel,parent_id,owner_id,note,"
                    "created_by,created_at,updated_at,offset_kind,client_token) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tx_date, direction, parent, cat, cents, shared, reimbursed, pending,
                     pending_use, travel, parent_id, owner_id, (body.get("note") or "")[:100],
                     u["id"], now_iso(), now_iso(), offset_kind, token))

                if parent_id:
                    used2 = used + cents
                    # 冲抵金额记到分配表上——这是金额归属的唯一真相
                    conn.execute(
                        "INSERT INTO offset_links(offset_id,parent_id,amount_cents,created_at) "
                        "VALUES(?,?,?,?)", (cur.lastrowid, parent_id, cents, now_iso()))
                    sync_parent_state(conn, [parent_id])
                    if used2 >= orig["amount_cents"]:
                        # 全额冲抵完（退货也算）：这张单既不用等用券，也不用再等报销了
                        if orig["reimb_pending"] or orig["pending_use"]:
                            conn.execute(
                                "UPDATE transactions SET reimb_pending=0, pending_use=0, "
                                "updated_at=? WHERE id=?", (now_iso(), parent_id))
                            cleared = True
                # 注意：必须走出 with（提交事务）之后再写响应，
                # 否则客户端可能在提交前就发起下一次读，读不到刚写的数据。
                result = {"ok": True, "id": cur.lastrowid, "cleared": cleared}
        except sqlite3.IntegrityError:
            # 并发同 token：两路都通过了上面那道预查，其中一路被唯一索引挡下来。
            # 这不是错误，就是「同一次提交到达了两次」，按重复处理返回先落库的那条。
            if token and self._find_by_token(token):
                return self._dup_result(token)
            raise
        return self._json(result)

    def _reimburse(self, tx_id, u, body):
        """报销到账：生成一条挂原单的冲抵记录，并把原单标成已报销。

        为什么不是「改个标记」：钱确实回到了账上，却不动任何金额，结余会永远偏低。
        这么做之后净支出会减少（和退款同一个口径：按**原单所在月**冲抵），
        「收入」一栏不动——报销是把自己垫的钱收回来，不是挣的。

        金额默认冲完剩余可冲抵额；公司只报一部分时把差额留在支出里，当成自付。
        """
        # 「到账」按钮也可能被连点或断网重发，同样走幂等键——前端按按钮实例生成，
        # 同一个按钮反复点复用同一个 token；列表刷新后按钮重画，token 自然换新。
        token = str(body.get("client_token") or "").strip()[:64] or None
        if token and self._find_by_token(token):
            return self._dup_result(token)
        try:
            with db() as conn:
                orig = conn.execute(
                    "SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
                if not orig:
                    return self._err("记录不存在", 404)
                if orig["direction"] != "expense":
                    return self._err("只有支出可以报销")
                used = offset_used(conn, tx_id)
                remain = orig["amount_cents"] - used
                if remain <= 0:
                    return self._err("这笔已经全部冲抵完了")
                try:
                    cents = int(body.get("amount_cents") or remain)
                except Exception:
                    return self._err("金额不合法")
                if cents <= 0 or cents > remain:
                    return self._err(f"最多还能冲抵 {remain / 100:.2f} 元")
                cur = conn.execute(
                    "INSERT INTO transactions(tx_date,direction,cat_parent,category,amount_cents,"
                    "shared,reimbursed,reimb_pending,pending_use,travel,parent_id,owner_id,note,"
                    "created_by,created_at,updated_at,offset_kind,client_token) "
                    "VALUES(?,?,?,?,?,0,0,0,0,0,?,?,?,?,?,?,'reimb',?)",
                    (datetime.now(TZ).date().isoformat(), "refund",
                     orig["cat_parent"], orig["category"], cents, tx_id, orig["owner_id"],
                     "报销到账", u["id"], now_iso(), now_iso(), token))
                conn.execute(
                    "INSERT INTO offset_links(offset_id,parent_id,amount_cents,created_at) "
                    "VALUES(?,?,?,?)", (cur.lastrowid, tx_id, cents, now_iso()))
                # 状态由记录驱动：这条支出已经收回钱 = 已报销
                sync_parent_state(conn, [tx_id])
                new_id = cur.lastrowid
        except sqlite3.IntegrityError:
            if token and self._find_by_token(token):
                return self._dup_result(token)
            raise
        return self._json({"ok": True, "id": new_id, "amount_cents": cents, "remain_cents": remain - cents})

    # ---- 冲抵分配 ----
    def _reimb_candidates(self, owner_id):
        """报销候选：只列你标了「待报销」的支出，不按金额筛、不限时间窗口。

        和退款候选是两条路，别合并：
        - 报销是「一次到账摊多笔」，单笔金额必然小于到账额，按「单笔剩余 ≥ 到账额」
          筛的话，填 5000 时 5 笔待报销会一笔都不出现（2026-10-28 实测）
        - 只认 reimb_pending，否则近 30 天任何有余额的消费都会被推上来当候选
        - 待报销全库也就几笔，不设窗口反而好找；差旅报销跨月是常态
        """

        def fetch(who):
            sql = ("SELECT t.id, t.tx_date, t.cat_parent, t.category, t.note, "
                   "t.amount_cents, t.owner_id, u.display_name AS owner_name, "
                   "(SELECT COALESCE(SUM(l.amount_cents),0) FROM offset_links l "
                   " WHERE l.parent_id=t.id) AS offset_cents "
                   "FROM transactions t JOIN users u ON u.id=t.owner_id "
                   "WHERE t.direction='expense' AND t.reimb_pending=1 AND t.reimbursed=0")
            args = []
            if who is not None:
                sql += " AND t.owner_id=?"
                args.append(who)
            sql += " ORDER BY t.tx_date DESC, t.id DESC LIMIT 200"
            with db() as conn:
                return [dict(r) for r in conn.execute(sql, args)]

        rows = fetch(owner_id)
        relaxed = False
        # 按「归属」过滤之后一条都没有，就放宽到全部人：管理员这类名下没账单的账号
        # 否则永远看不到候选（2026-10-28 就是这么卡住的）
        if owner_id is not None and not rows:
            rows = fetch(None)
            relaxed = bool(rows)

        items = []
        for r in rows:
            remain = r["amount_cents"] - (r["offset_cents"] or 0)
            if remain <= 0:
                continue
            r.update(remain_cents=remain, diff_cents=remain, match="reimb")
            items.append(r)
        return self._json({
            "items": items,
            "auto": None,             # 多选场景不预选单笔：猜错一笔就是整笔账错
            "mode": "reimb",
            "sum_cents": sum(x["remain_cents"] for x in items),
            "relaxed": relaxed,
        })

    def _get_links(self, tx_id):
        """一条冲抵的分配明细。未分配部分（到账了还没认领）统计上按无主算。"""
        with db() as conn:
            row = conn.execute(
                "SELECT id, tx_date, amount_cents, offset_kind, note, cat_parent, category "
                "FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if not row:
                return self._err("记录不存在", 404)
            items = []
            for r in conn.execute(
                    "SELECT l.id AS link_id, l.parent_id, l.amount_cents AS alloc_cents, "
                    "p.tx_date, p.cat_parent, p.category, p.note, p.amount_cents AS parent_cents, "
                    "(SELECT COALESCE(SUM(x.amount_cents),0) FROM offset_links x "
                    " WHERE x.parent_id=p.id AND x.offset_id<>?) AS used_other "
                    "FROM offset_links l JOIN transactions p ON p.id=l.parent_id "
                    "WHERE l.offset_id=? ORDER BY p.tx_date, l.id", (tx_id, tx_id)):
                d = dict(r)
                # 本笔最多还能占这笔支出多少（原额扣掉别的冲抵已占用的）
                d["max_cents"] = max(0, d["parent_cents"] - d["used_other"])
                items.append(d)
            allocated = sum(x["alloc_cents"] for x in items)
            offset = dict(row)
            offset.pop("client_token", None)      # 内部幂等键，不外泄
            return self._json({
                "offset": offset,
                "items": items,
                "allocated": allocated,
                "unallocated": row["amount_cents"] - allocated,
            })

    def _save_links(self, tx_id, u, body):
        """整体替换一条冲抵的分配。金额守恒和原单额度都过 check_links。"""
        items = body.get("items")
        if not isinstance(items, list):
            return self._err("items 必须是数组")
        pairs = []
        try:
            for it in items:
                pairs.append((int(it.get("parent_id")), int(it.get("amount_cents"))))
        except Exception:
            return self._err("每条分配都要带 parent_id 和 amount_cents")
        with db() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if not row:
                return self._err("记录不存在", 404)
            if row["direction"] != "refund":
                return self._err("只有退款 / 报销记录才能分配")
            clean, err = check_links(conn, tx_id, row["amount_cents"], pairs)
            if err:
                return self._err(err)
            touched = replace_links(conn, tx_id, clean)
            # 主原单只用于列表显示，取分配金额最大的那条。
            # 分类 / 归属得跟着它一起换——明细行标题就是这一格的分类，
            # 改完分配还留着旧主原单的分类，标题会指向一笔已经不是"主要"的账单。
            main = max(clean, key=lambda p: p[1])[0] if clean else None
            if main:
                mrow = conn.execute("SELECT cat_parent, category, owner_id FROM transactions "
                                    "WHERE id=?", (main,)).fetchone()
                conn.execute(
                    "UPDATE transactions SET parent_id=?, cat_parent=?, category=?, owner_id=?, "
                    "updated_at=? WHERE id=?",
                    (main, mrow["cat_parent"], mrow["category"], mrow["owner_id"],
                     now_iso(), tx_id))
            else:
                conn.execute("UPDATE transactions SET parent_id=?, updated_at=? WHERE id=?",
                             (None, now_iso(), tx_id))
            sync_parent_state(conn, touched)
            total = row["amount_cents"]
            allocated = sum(c for _, c in clean)
        return self._json({"ok": True, "allocated": allocated,
                           "unallocated": total - allocated})

    # ---- PATCH ----
    def _api_patch(self, path):
        u = self._require()
        if not u:
            return
        m = re.fullmatch(r"/api/loans/(\d+)", path)
        if m:
            return self._patch_loan(int(m.group(1)), u)
        m = re.fullmatch(r"/api/tx/(\d+)", path)
        if not m:
            return self._err("not found", 404)
        tx_id = int(m.group(1))
        body = self._body()
        if getattr(self, "_bad_body", False):
            return self._err("请求体不是合法的 JSON 对象", code="bad_body")
        with db() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if not row:
                return self._err("记录不存在", 404)
            direction = body.get("direction", row["direction"])
            if direction not in TREES:
                return self._err("收支方向非法", code="category_invalid")
            # 日期必须显式判断 key：body.get("tx_date", row["tx_date"]) 在
            # 「key 存在但值为空」时返回空串，strptime 一失败旧代码就把它原样写进库，
            # 记录从此从所有月度视图里消失（R1）。
            try:
                tx_date_final = (parse_tx_date(body["tx_date"]) if "tx_date" in body
                                 else row["tx_date"])
            except ValueError as e:
                return self._err(str(e), code=getattr(e, "code", "date_invalid"))
            # 「报销状态」不是能随手翻的开关：有报销到账记录时，两个标记都锁死，只能删那条记录来撤回。
            # 打开「待报销」等于宣布「还没报」，可那条到账记录明明还挂在账上；
            # 关掉「已报销」则会让标记和记录各说各的——用户干过一次，原单标回未报销、冲抵记录还挂着。
            if row["direction"] == "expense":
                want_unset = "reimbursed" in body and not body.get("reimbursed")
                want_pending = "reimb_pending" in body and body.get("reimb_pending")
                want_set = "reimbursed" in body and bool(body.get("reimbursed"))
                if want_unset or want_pending or want_set:
                    has_reimb = conn.execute(
                        "SELECT 1 FROM transactions WHERE parent_id=? AND offset_kind='reimb' "
                        "LIMIT 1", (tx_id,)).fetchone()
                    if has_reimb and (want_unset or want_pending):
                        return self._err("这笔有报销到账记录，先删掉那条记录再改标记")
                    # 反向也锁：没有到账记录就不许手点「已报销」——它意味着钱已经回来了，
                    # 可净支出一分没减，月底对账只会怀疑是谁算错了（R14）。
                    if want_set and not has_reimb:
                        return self._err(
                            "「已报销」不能手动打开：它表示这笔钱已经收回来。"
                            "请记一笔「退款 / 报销」到账，系统会自动标记并冲减支出。",
                            code="reimburse_requires_record")
            # 先定冲抵关系：挂着的退款，分类与归属一律跟随原单，
            # 否则会拿「餐饮」去撞退款分类树而报错。
            if "parent_id" in body:
                try:
                    parent_id = parse_opt_int(body.get("parent_id"), "原账单")
                except ValueError as e:
                    return self._err(str(e), code="parent_id_invalid")
            else:
                parent_id = row["parent_id"]
            try:
                owner_final = int(body["owner_id"]) if "owner_id" in body else row["owner_id"]
            except Exception:
                return self._err("归属用户不合法")
            try:
                cents = parse_cents(body.get("amount_cents", row["amount_cents"]))
            except ValueError as e:
                return self._err(str(e), code=getattr(e, "code", "amount_invalid"))

            # 有子冲抵的支出不许改收支方向：冲抵记录挂在它身上，方向一改，
            # 那 20 元既不在 refund_offset 也不在 refund_orphan 里——钱凭空消失，
            # 界面还一声不吭（R5）。两处都要查：offset_links 是金额真相，
            # transactions.parent_id 可能是没有分配的历史残留。
            if row["direction"] == "expense" and direction != "expense":
                has_child = conn.execute(
                    "SELECT 1 FROM offset_links WHERE parent_id=? LIMIT 1",
                    (tx_id,)).fetchone() or conn.execute(
                    "SELECT 1 FROM transactions WHERE parent_id=? LIMIT 1",
                    (tx_id,)).fetchone()
                if has_child:
                    return self._err("这笔已有退款/报销冲抵，先把冲抵记录删掉再改收支方向",
                                     code="direction_has_offsets")

            # 已冲抵额是硬地板。支出 200 已退 150，再把金额改成 100：净支出变成 -50，
            # 分类饼图整块消失，而且没有任何代码会再纠正它——只能人工发现（R2）。
            # 用 row["direction"]（原方向）判定：本次同时改方向时由下面那道检查负责。
            if row["direction"] == "expense":
                already = offset_used(conn, tx_id)
                if already and cents < already:
                    return self._err("这笔已冲抵 %.2f 元，金额不能低于它" % (already / 100.0),
                                     code="amount_below_offset")

            orig = None
            used = 0
            # 这条记录自己已经认领了多少钱：金额归属的唯一真相是 offset_links，
            # 不是 parent_id（parent_id 只是「主原单，供列表显示」）。必须先取，
            # 否则下面会拿主原单的剩余额度去卡整笔到账额——多笔分摊时那个数天生小于总额，
            # 结果连「只改个备注」都会被拒（2026-10-29 实测：摊 3 笔的 300 元报销，
            # 保存报「最多还能退 200.00 元」）。
            link_n, link_sum = 0, 0
            if row["direction"] == "refund":
                link_n, link_sum = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM offset_links "
                    "WHERE offset_id=?", (tx_id,)).fetchone()
            if parent_id:
                if parent_id == tx_id:
                    return self._err("不能冲抵自己")
                if direction != "refund":
                    return self._err("只有退款可以冲抵原账单")
                orig = conn.execute(
                    "SELECT * FROM transactions WHERE id=?", (parent_id,)).fetchone()
                if not orig or orig["direction"] != "expense":
                    return self._err("原账单不存在或不是支出")
                used = conn.execute(
                    "SELECT COALESCE(SUM(l.amount_cents),0) FROM offset_links l "
                    "WHERE l.parent_id=? AND l.offset_id<>?", (parent_id, tx_id)).fetchone()[0]
                remain = orig["amount_cents"] - used
                if not link_n:
                    # 还没分配过：金额就是退款额，必须落在原单剩余额度里
                    if remain <= 0:
                        return self._err("这笔已经全部退完了")
                    if cents > remain:
                        return self._err(f"最多还能退 {remain / 100:.2f} 元")
                elif link_n == 1 and cents > remain and cents > row["amount_cents"]:
                    # 单笔：往上调时仍要卡原单剩余（防手滑多打一个 0）；
                    # 本来就超出（差补留了未分配那种）的旧数据不能因此存不了
                    return self._err(f"最多还能退 {remain / 100:.2f} 元")
                parent, cat = orig["cat_parent"], orig["category"]
                # 归属无条件跟原单走：冲抵记录是原单的影子，没有独立归属。
                # 允许改会造出「明细算在 A 头上、统计按 B 抵扣」的两套账（R8）。
                # 不报错而是静默忽略：前端整包提交表单时会带上这个字段，
                # 报错等于让用户莫名保存失败。
                owner_final = orig["owner_id"]
            else:
                parent = body.get("cat_parent", row["cat_parent"])
                cat = body.get("category", row["category"])
                if parent not in TREES[direction] or cat not in TREES[direction][parent]:
                    # 解除冲抵时，从支出继承来的分类在退款树里没有，
                    # 回落到退款树的第一个大类第一个子类（分类可配置后不能写死名字）
                    if direction == "refund" and "cat_parent" not in body:
                        rp = next(iter(TREES["refund"]))
                        parent, cat = rp, TREES["refund"][rp][0]
                    else:
                        return self._err("分类不在允许范围内", code="category_invalid")
            shared = 1 if body.get("shared", row["shared"]) else 0
            pending = 1 if body.get("reimb_pending", row["reimb_pending"]) else 0
            reimbursed = 1 if body.get("reimbursed", row["reimbursed"]) else 0
            pending_use = 1 if body.get("pending_use", row["pending_use"]) else 0
            travel = 1 if body.get("travel", row["travel"]) else 0
            # 互斥：本次显式打开哪个，就把另一个关掉
            if pending and reimbursed:
                if "reimb_pending" in body:
                    reimbursed = 0
                else:
                    pending = 0
            if direction != "expense":
                # 5 个标记只属于支出，一律清空（曾经漏了 reimbursed，R6）。
                # 这里是"静默清 0"而不是报错：它们对非支出本就是无意义字段，
                # 前端表单可能整包提交，报错只会让用户莫名保存不了。
                shared = pending = reimbursed = 0
                pending_use = travel = 0
            # 冲抵记录的分配要跟金额 / 主原单保持一致。多笔分配时不许在这里改金额——
            # 改了会把摊到各笔上的钱冲散，只能去「冲抵分配」里逐笔调。
            if direction == "refund":
                # 分摊在多笔上的冲抵，金额一改就把各笔的钱冲散了，只能去「冲抵分配」里调。
                # 单笔的无所谓，分配额跟着新金额走（多出来的部分自动压缩）。
                if link_n > 1 and cents != row["amount_cents"]:
                    return self._err("这笔冲抵分摊在 %d 笔账单上，请到「冲抵分配」里改金额" % link_n)
                if link_sum > cents:
                    if link_n > 1:
                        return self._err("已分配 %.2f 元，超过新金额 %.2f，请先到「冲抵分配」里调整"
                                         % (link_sum / 100.0, cents / 100.0))
                    link_sum = cents
            conn.execute(
                "UPDATE transactions SET tx_date=?,direction=?,cat_parent=?,category=?,"
                "amount_cents=?,shared=?,reimbursed=?,reimb_pending=?,pending_use=?,travel=?,"
                "parent_id=?,owner_id=?,note=?,updated_at=?,updated_by=? WHERE id=?",
                (tx_date_final, direction, parent, cat, cents,
                 shared, reimbursed, pending, pending_use, travel, parent_id, owner_final,
                 (body.get("note", row["note"]) or "")[:100],
                 now_iso(), u["id"], tx_id))
            # 分配表跟着走：解除冲抵 → 清空；单笔 → 同步金额；多笔 → 原样保留
            touched = set()
            if direction != "refund":
                if row["direction"] == "refund":
                    touched = replace_links(conn, tx_id, [])
            elif not parent_id:
                touched = replace_links(conn, tx_id, [])
            elif link_n <= 1:
                touched = replace_links(conn, tx_id, [(parent_id, link_sum or cents)])
            if touched:
                sync_parent_state(conn, touched)
            if orig and used + cents >= orig["amount_cents"]:
                conn.execute(
                    "UPDATE transactions SET pending_use=0, updated_at=? "
                    "WHERE id=?", (now_iso(), parent_id))
        return self._json({"ok": True})

    def _patch_loan(self, loan_id, u):
        """改借款 / 记还款。repaid_cents 传绝对值，还满自动标记结清。"""
        body = self._body()
        with db() as conn:
            row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if not row:
                return self._err("借款不存在", 404)
            try:
                direction = body.get("direction", row["direction"])
                if direction not in ("out", "in"):
                    raise ValueError("借款方向只能是 out 或 in")
                counterparty = (body.get("counterparty", row["counterparty"]) or "").strip()
                if not counterparty:
                    raise ValueError("对方不能为空")
                cents = parse_cents(body.get("amount_cents", row["amount_cents"]))
                repaid = int(body.get("repaid_cents", row["repaid_cents"]) or 0)
                if repaid < 0 or repaid > cents:
                    raise ValueError("已还金额必须在 0 到借款金额之间")
                # 金额允许 0（表示"还没还"），所以 repaid_cents 不走 parse_cents；
                # 日期与建账、改账同一个解析函数，别在这里留第二套口径。
                loan_date = (parse_tx_date(body["loan_date"]) if "loan_date" in body
                             else row["loan_date"])
                owner_id = int(body.get("owner_id", row["owner_id"]))
            except ValueError as e:
                return self._err(str(e), code=getattr(e, "code", "bad_body"))
            except Exception:
                return self._err("参数不合法，请检查后重试", code="bad_body")
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone():
                return self._err("归属用户不存在")
            settled = repaid >= cents
            conn.execute(
                "UPDATE loans SET direction=?,counterparty=?,amount_cents=?,repaid_cents=?,"
                "loan_date=?,note=?,owner_id=?,settled_at=?,updated_at=? WHERE id=?",
                (direction, counterparty[:20], cents, repaid, loan_date,
                 (body.get("note", row["note"]) or "")[:100], owner_id,
                 now_iso() if settled else None, now_iso(), loan_id))
        return self._json({"ok": True, "settled": settled})

    # ---- DELETE ----
    def _api_delete(self, path):
        u = self._require()
        if not u:
            return
        m = re.fullmatch(r"/api/loans/(\d+)", path)
        if m:
            lid = int(m.group(1))
            with db() as conn:
                lrow = conn.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
                if not lrow:
                    return self._err("借款不存在", 404)
                audit_delete(conn, u, "loans", lrow)
                conn.execute("DELETE FROM loans WHERE id=?", (lid,))
            return self._json({"ok": True})
        m = re.fullmatch(r"/api/tx/(\d+)", path)
        if not m:
            return self._err("not found", 404)
        tx_id = int(m.group(1))
        with db() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if not row:
                return self._err("记录不存在", 404)
            # 删之前先把整行 + 它摊到哪几笔上存进审计。分配明细会被 CASCADE 清掉，
            # 不一起存就只剩「某天删了一条冲抵」，等于没记。
            audit_delete(conn, u, "transactions", row, {"links": [dict(r) for r in conn.execute(
                "SELECT offset_id,parent_id,amount_cents,created_at FROM offset_links "
                "WHERE offset_id=?", (tx_id,))]})
            # 这条记录自己摊到了哪些原单上（删之前先记下来，删完要重算它们的报销状态）
            touched = {r["parent_id"] for r in conn.execute(
                "SELECT parent_id FROM offset_links WHERE offset_id=?", (tx_id,))}
            # 删掉原单后，挂在它下面的冲抵变成「无主退款 / 无主报销」，不会连带删除。
            # 分配表里指向它的行由 ON DELETE CASCADE 清掉——那笔钱回到「未分配」。
            detached = conn.execute(
                "UPDATE transactions SET parent_id=NULL WHERE parent_id=?", (tx_id,)).rowcount
            conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
            # 删掉「报销到账」= 撤回这次到账：受影响的原单按剩余分配重算，
            # 最后一条报销没了就退回「待报销」。状态跟着记录走，不留解释不了的标记。
            if row and row["direction"] == "refund" and touched:
                sync_parent_state(conn, touched)
            restored = len([p for p in touched if p])
        return self._json({"ok": True, "detached_refunds": detached, "reimb_restored": restored})


def ensure_seed_user():
    """首次启动建一个管理员，密码只打印一次到日志。

    以前还会写一份 data/initial_admin.txt。问题是它一直留在那儿：备份包里有、翻目录看得见，
    而管理员可能当天就改过密码了，留着只是多一处明文。现在只在 stdout 打一次
    （systemd 里就是 journalctl），要指定初始密码就设 LEDGER_ADMIN_PASSWORD。
    """
    with db() as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        pwd = (os.environ.get("LEDGER_ADMIN_PASSWORD") or "").strip() or secrets.token_urlsafe(9)
        conn.execute(
            "INSERT INTO users(username,display_name,pwd_hash,is_admin,created_at) "
            "VALUES(?,?,?,1,?)", ("admin", "管理员", hash_password(pwd), now_iso()))
        print("=" * 60, flush=True)
        print(f"初次启动 管理员账号 admin / {pwd}", flush=True)
        print("这行只出现这一次，请立刻记下来并改成自己的密码。", flush=True)
        print("（不再写 data/initial_admin.txt；忘了就删库重来或直接改 users 表）", flush=True)
        print("=" * 60, flush=True)


def main():
    global _DB_CONN

    init_db()
    ensure_seed_user()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def _graceful(signum, frame):
        # 必须在另一个线程里调 shutdown()：在 serve_forever 所在线程里调会自锁。
        threading.Thread(target=server.shutdown, daemon=True).start()

    # systemd 停服务发的是 SIGTERM，而 Python 的默认处置是「直接终止进程」——
    # 不跑 finally、不跑 atexit、不触发析构，sqlite 连接于是永远不会 close。
    # 偏偏 SQLite 只在「最后一个连接正常关闭」时才做收尾 checkpoint，所以每次
    # 部署重启都把上一轮的 -wal 原样留给下一次，WAL 只增不减。
    # 2026-11-03 实测：主库 164KB 停在 10-29，11 月的 31 笔全在 4MB 的 -wal 里，
    # 裸拷 ledger.db 正好丢这 31 笔。接住信号后每次重启都会收敛回主库。
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    print(f"ledger listening on 127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    finally:
        # 显式 checkpoint 比只靠 close() 稳：万一还有残留线程握着连接，
        # close() 不一定能走完收尾。锁在这里是为了不和在途请求抢连接。
        server.server_close()
        with _DB_LOCK:
            if _DB_CONN is not None:
                try:
                    _DB_CONN.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    _DB_CONN.close()
                except Exception:
                    pass
                _DB_CONN = None


if __name__ == "__main__":
    main()
