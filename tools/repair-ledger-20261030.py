# -*- coding: utf-8 -*-
"""家庭账本存量脏数据体检 / 修复（一次性脚本，2026-10-30）

用法：
    python3 tools/repair-ledger-20261030.py --db /opt/ledger/data/ledger.db
        默认 dry-run：只打印「将改哪些行」，一个字节都不写。
    python3 tools/repair-ledger-20261030.py --db /opt/ledger/data/ledger.db --apply
        真正写库。**必须先看过 dry-run 清单、并且人逐类点头**再跑。

为什么单独一个脚本、还默认只读：
数据修复没有「撤销键」。dry-run 打开的是 sqlite 的 `mode=ro` URI，就算代码里有
写语句也写不进去——这是唯一能保证「看一眼不会改坏」的做法。

四类问题与处置（对应改进计划 D-05）：
  1. 空/非法 tx_date        → 用 created_at 的日期回填；created_at 也不可用就列出来等人工，不猜
  2. 超冲抵（分配>原单额）  → 只列清单。改金额还是删分配是人的判断，脚本不替用户决定
  3. 非支出带支出标记       → 直接清 0（低风险，纯冗余字段）
  4. 悬空 / 非法分配         → 删掉这条分配，并把原单的报销状态重算一次
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

MARKS = ("shared", "reimb_pending", "pending_use", "travel", "reimbursed")
DATE_OK = "length(tx_date)=10 AND tx_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def open_db(path, readonly):
    if not os.path.exists(path):
        sys.exit("找不到数据库：%s" % path)
    if readonly:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def valid_date(s):
    try:
        datetime.strptime((s or "")[:10], "%Y-%m-%d")
        return s[:10]
    except Exception:
        return None


def scan(con):
    """把四类问题的处置写成「一条 SQL + 建议值」的清单，dry-run 与 apply 共用。"""
    out = {"bad_date": [], "over_offset": [], "marks": [], "links": []}

    # 1. 空 / 非法日期
    for r in con.execute("SELECT id, tx_date, created_at FROM transactions WHERE NOT (%s)" % DATE_OK):
        fix = valid_date(r["created_at"])
        out["bad_date"].append({"id": r["id"], "now": r["tx_date"], "to": fix or "",
                                "why": "created_at[0:10]" if fix else "created_at 不可用，需人工"})

    # 2. 超冲抵：分配合计超过原单金额（金额是唯一真相，不能靠改业务数据凑）
    for r in con.execute(
            "SELECT p.id, p.tx_date, p.amount_cents, SUM(l.amount_cents) s "
            "FROM transactions p JOIN offset_links l ON l.parent_id=p.id "
            "GROUP BY p.id HAVING SUM(l.amount_cents) > p.amount_cents"):
        out["over_offset"].append({"id": r["id"], "now": r["amount_cents"],
                                   "to": r["s"], "why": "分配合计 %d > 原单金额 %d，需人工决定"
                                   % (r["s"], r["amount_cents"])})

    # 3. 非支出带着支出标记（纯冗余，清 0 不影响任何金额）
    cond = " OR ".join("%s=1" % m for m in MARKS)
    for r in con.execute("SELECT id, direction, %s FROM transactions "
                         "WHERE direction!='expense' AND (%s)" % (",".join(MARKS), cond)):
        on = [m for m in MARKS if r[m]]
        out["marks"].append({"id": r["id"], "now": ",".join(on), "to": "",
                             "why": "%s 不是支出，标记清 0" % r["direction"]})

    # 4. 悬空 / 非法分配：指向不存在的记录、或指向了非退款/非支出
    for r in con.execute(
            "SELECT l.id, l.offset_id, l.parent_id, l.amount_cents, "
            " o.id o_ok, o.direction o_dir, p.id p_ok, p.direction p_dir "
            "FROM offset_links l LEFT JOIN transactions o ON o.id=l.offset_id "
            "LEFT JOIN transactions p ON p.id=l.parent_id "
            "WHERE o.id IS NULL OR p.id IS NULL OR o.direction!='refund' "
            "  OR p.direction!='expense' OR l.amount_cents<=0"):
        why = []
        if r["o_ok"] is None:
            why.append("冲抵记录已不存在")
        elif r["o_dir"] != "refund":
            why.append("冲抵记录方向是 %s" % r["o_dir"])
        if r["p_ok"] is None:
            why.append("原单已不存在")
        elif r["p_dir"] != "expense":
            why.append("原单方向是 %s" % r["p_dir"])
        if r["amount_cents"] <= 0:
            why.append("分配金额 %d" % r["amount_cents"])
        out["links"].append({"id": r["id"], "now": "offset=%s parent=%s %d 分"
                             % (r["offset_id"], r["parent_id"], r["amount_cents"]),
                             "to": "删", "why": "；".join(why)})
    return out


def report(plan):
    total = sum(len(v) for v in plan.values())
    print("=" * 72)
    print("家庭账本存量脏数据清单（%s）" % ("dry-run，未写库" if DRY else "APPLY，正在写入"))
    print("=" * 72)
    for key, title in (("bad_date", "1. 空/非法日期"), ("over_offset", "2. 超冲抵（只列，不自动改）"),
                       ("marks", "3. 非支出带支出标记"), ("links", "4. 悬空/非法分配（删）")):
        rows = plan[key]
        print("\n%s：%d 条" % (title, len(rows)))
        for r in rows[:20]:
            print("   id=%-6s 现值=%-28s → %-12s 依据=%s"
                  % (r["id"], str(r["now"])[:28], r["to"] or "（清空）", r["why"]))
        if len(rows) > 20:
            print("   …… 另有 %d 条，见 --csv" % (len(rows) - 20))
    print("\n合计 %d 条需要处置。" % total)
    return total


def apply(con, plan):
    changed = 0
    for r in plan["bad_date"]:
        if not r["to"]:
            continue                     # created_at 也不可用 → 不动，留人工
        con.execute("UPDATE transactions SET tx_date=? WHERE id=?", (r["to"], r["id"]))
        changed += 1
    for r in plan["marks"]:
        con.execute("UPDATE transactions SET " + ",".join("%s=0" % m for m in MARKS)
                    + " WHERE id=?", (r["id"],))
        changed += 1
    for r in plan["links"]:
        con.execute("DELETE FROM offset_links WHERE id=?", (r["id"],))
        changed += 1
    # 删分配会改变原单的「已报销」状态：有 reimb 分配 → 已报销；没有 → 退回未报销。
    # 只动被碰到过的原单，不碰全库（这一步错了也只影响这几笔）。
    touched = {r["now"].split("parent=")[1].split()[0] for r in plan["links"]
               if "parent=" in str(r["now"])}
    for pid in [int(x) for x in touched if str(x).isdigit()]:
        has = con.execute("SELECT 1 FROM transactions WHERE parent_id=? AND offset_kind='reimb' "
                          "LIMIT 1", (pid,)).fetchone()
        con.execute("UPDATE transactions SET reimbursed=? WHERE id=? AND direction='expense'",
                    (1 if has else 0, pid))
    con.commit()
    print("\n已写入 %d 处改动。请立刻复跑体检 SQL（Z-01 的 4 条），命中数应为 0。" % changed)
    print("回滚：恢复部署前的备份包（ledger-backup.sh 产出的 tar.gz）。")


def main():
    global DRY
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("LEDGER_HOME", "/opt/ledger"),
                                                 "data", "ledger.db"))
    ap.add_argument("--apply", action="store_true", help="真正写库；不加就是 dry-run")
    args = ap.parse_args()
    DRY = not args.apply

    con = open_db(args.db, readonly=DRY)
    plan = scan(con)
    total = report(plan)

    if total == 0:
        print("\n生产库是干净的，没有要修的行。这份「0 改动」输出本身就是验收证据。")
        return 0
    if DRY:
        print("\n以上为 dry-run。确认无误后加 --apply（先备份！）：")
        print("   ssh your-server '/usr/local/bin/ledger-backup.sh'")
        return 0
    apply(con, plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
