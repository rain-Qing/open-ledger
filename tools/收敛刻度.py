#!/usr/bin/env python3
"""把账本前端的字号/圆角/字重/间距字面量收敛到设计令牌。

刻度表见 docs/设计规范.md §2.2 紧凑刻度。本脚本只做替换，不改结构：
  字号 16 种 → 8 级    圆角 11 种 → 5 级
  字重  6 种 → 3 级    间距 18 种 → 10 级

用法：
  python tools/收敛刻度.py            # 就地改写 ledger/www/index.html
  python tools/收敛刻度.py --dry-run  # 只报告不写

只处理 <style> … </style> 之间的 CSS；JS 区没有字号字面量（已核实），不动。
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "ledger" / "www" / "index.html"

FS = {10.5: "--fs-micro", 11: "--fs-micro", 11.5: "--fs-aux", 12: "--fs-aux",
      12.5: "--fs-sm", 13: "--fs-sm", 13.5: "--fs-body", 14: "--fs-body",
      15: "--fs-em", 16: "--fs-em", 17: "--fs-title", 20: "--fs-title",
      22: "--fs-title", 30: "--fs-num", 32: "--fs-num", 44: "--fs-hero"}

RAD = {"3": "--r-xs", "5": "--r-xs", "6": "--r-xs", "7": "--r-xs",
       "8": "--r-sm", "9": "--r-sm", "10": "--r-sm",
       "11": "--r-md", "12": "--r-md", "18": "--r-lg", "999": "--r-pill"}

FW = {"400": "--fw", "500": "--fw-mid", "600": "--fw-bold",
      "650": "--fw-bold", "700": "--fw-bold", "800": "--fw-bold"}

SP = {1: "--sp-1", 2: "--sp-1", 3: "--sp-1", 4: "--sp-2", 5: "--sp-2",
      6: "--sp-3", 7: "--sp-3", 8: "--sp-4", 9: "--sp-4", 10: "--sp-5",
      11: "--sp-5", 12: "--sp-6", 13: "--sp-6", 14: "--sp-7", 16: "--sp-8",
      18: "--sp-8", 20: "--sp-8", 22: "--sp-9", 24: "--sp-9", 28: "--sp-9",
      32: "--sp-10", 44: "--sp-10"}

stats = {}


def bump(key):
    stats[key] = stats.get(key, 0) + 1


def fix_font_size(css):
    def repl(m):
        v = float(m.group(1))
        if v in FS:
            bump("字号")
            return "font-size:var(%s)" % FS[v]
        return m.group(0)
    return re.sub(r"font-size[ ]*:[ ]*([0-9.]+)px", repl, css)


def fix_radius(css):
    def repl(m):
        v = m.group(1)
        if v in RAD:
            bump("圆角")
            return "border-radius:var(%s)" % RAD[v]
        return m.group(0)
    return re.sub(r"border-radius[ ]*:[ ]*([0-9]+)px", repl, css)


def fix_weight(css):
    def repl(m):
        v = m.group(1)
        if v in FW:
            bump("字重")
            return "font-weight:var(%s)" % FW[v]
        return m.group(0)
    return re.sub(r"font-weight[ ]*:[ ]*([0-9]+)(?=[;}])", repl, css)


SP_RE = re.compile(r"(?<![-a-zA-Z])(padding|margin|gap)(-(?:top|right|bottom|left))?"
                   r"[ ]*:[ ]*([^;{}]+)(?=[;}])")


def fix_spacing(css):
    def one_unit(m):
        raw = m.group(1)
        n = float(raw)
        if n != int(n) or n <= 0:
            return m.group(0)
        var = SP.get(int(n))
        if not var:
            return m.group(0)
        bump("间距")
        return "var(%s)" % var

    def repl(m):
        prop, side, val = m.group(1), m.group(2) or "", m.group(3)
        new_val = re.sub(r"([0-9]+(?:\.[0-9]+)?)px", one_unit, val)
        return "%s%s:%s" % (prop, side, new_val)

    return SP_RE.sub(repl, css)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = PAGE.read_text(encoding="utf-8")
    start = text.index("<style>")
    end = text.index("</style>", start)
    css = text[start:end]

    css = fix_font_size(css)
    css = fix_radius(css)
    css = fix_weight(css)
    css = fix_spacing(css)

    out = text[:start] + css + text[end:]

    print("替换统计：")
    for k in ("字号", "圆角", "字重", "间距"):
        print("  %-4s %3d 处" % (k, stats.get(k, 0)))
    print()

    left = {
        "字号": re.findall(r"font-size[ ]*:[ ]*[0-9.]+px", css),
        "圆角": re.findall(r"border-radius[ ]*:[ ]*[0-9]+px", css),
        "字重": re.findall(r"font-weight[ ]*:[ ]*[0-9]+(?=[;}])", css),
        "间距": re.findall(r"(?<![-a-zA-Z])(?:padding|margin|gap)(?:-\w+)?[ ]*:[ ]*[^;{}]*[0-9]+px[^;{}]*(?=[;}])", css),
    }
    bad = False
    for k, v in left.items():
        print("剩余%s字面量：%d" % (k, len(v)))
        for x in v[:6]:
            print("    ", x.strip()[:78])
        if v:
            bad = True

    if args.dry_run:
        print("\n（dry-run，未写文件）")
        return 0

    PAGE.write_text(out, encoding="utf-8")
    print("\n已写入 %s" % PAGE.relative_to(ROOT))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
