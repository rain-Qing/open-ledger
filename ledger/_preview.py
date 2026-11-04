# -*- coding: utf-8 -*-
"""生成本地预览副本（伪造 /api 响应）+ 无头 Edge 截图，用来验收界面。
用法：python _preview.py            生成 html 并截图到 _preview/
"""
import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).parent
OUT = HERE / "_preview"
OUT.mkdir(exist_ok=True)
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

MOCK = r"""
<script>
const MOCK_USERS = [{id:2, username:"member_a", display_name:"张三", is_admin:0},
                    {id:3, username:"member_b",   display_name:"李四", is_admin:0}];
const MOCK_ME = MOCK_USERS[0];
const MOCK_CATS = {
  expense: {"餐饮":["早餐","午餐","晚餐","外卖","买菜","零食饮品","其他"], "交通":["公交地铁","打车","加油","停车","火车飞机","其他"],
            "购物":["日用品","服饰","数码","家居","其他"], "生活缴费":["房租房贷","物业","水电燃气","宽带","话费","会员订阅","其他"],
            "医疗":["门诊","药品","体检","其他"], "娱乐":["电影","游戏","旅行","运动","其他"],
            "人情":["红包","礼物","请客","其他"], "其他支出":["其他","课程","书籍"]},
  income:  {"工资":["月度工资","奖金"], "补贴":["出差补贴","餐补","其他补贴"], "投资":["利息","理财收益"],
            "红包礼金":["收红包","礼金"], "其他收入":["其他"]},
  refund:  {"退款":["购物退款","服务退款","其他退款"]},
};
function mk(o){ return Object.assign({shared:0,reimb_pending:0,reimbursed:0,pending_use:0,
  parent_id:null,refunded_cents:0,refund_count:0,reimb_cents:0,note:"",refund_count_note:""}, o); }
const MOCK_TX = [
  mk({id:101, tx_date:"2026-10-24", direction:"expense", cat_parent:"购物", category:"日用",
      amount_cents:12000, pending_use:1, owner_id:2, owner_name:"张三", note:"火锅券", remain_cents:12000}),
  mk({id:102, tx_date:"2026-10-20", direction:"expense", cat_parent:"购物", category:"数码",
      amount_cents:60000, owner_id:2, owner_name:"张三", note:"京东耳机",
      refunded_cents:20000, refund_count:1, remain_cents:40000}),
  mk({id:103, tx_date:"2026-10-18", direction:"expense", cat_parent:"交通", category:"打车",
      amount_cents:3000, reimb_pending:1, travel:1, owner_id:3, owner_name:"李四",
      note:"打车去客户", remain_cents:3000}),
  mk({id:104, tx_date:"2026-10-15", direction:"expense", cat_parent:"餐饮", category:"外卖",
      amount_cents:4560, shared:1, owner_id:3, owner_name:"李四", note:"周末外卖", remain_cents:4560}),
  mk({id:105, tx_date:"2026-10-12", direction:"expense", cat_parent:"餐饮", category:"早餐",
      amount_cents:10000, owner_id:2, owner_name:"张三", note:"团购双人餐",
      refunded_cents:10000, refund_count:1, remain_cents:0}),
  mk({id:106, tx_date:"2026-10-11", direction:"refund", cat_parent:"餐饮", category:"早餐",
      amount_cents:5000, owner_id:2, owner_name:"张三", note:"退一份", parent_id:105,
      parent_date:"2026-10-12", parent_cat_parent:"餐饮", parent_category:"早餐",
      parent_amount:10000, link_count:2, link_total:5000, remain_cents:5000}),
  mk({id:107, tx_date:"2026-10-09", direction:"refund", cat_parent:"退款", category:"其他退款",
      amount_cents:2880, owner_id:3, owner_name:"李四", note:"不记得哪笔了", remain_cents:2880}),
  mk({id:108, tx_date:"2026-10-05", direction:"income", cat_parent:"工资", category:"月薪",
      amount_cents:1500000, owner_id:2, owner_name:"张三", note:"十月工资", remain_cents:1500000}),
  // 刻意不留备注：副标题会退化成一级大类（描边小标签），用来验收这个兜底形态
  mk({id:109, tx_date:"2026-10-04", direction:"expense", cat_parent:"交通", category:"公交地铁",
      amount_cents:200, owner_id:3, owner_name:"李四", remain_cents:200}),
  // 已报销且有到账记录的样本：编辑弹层里「待报销 / 已报销」两个开关都该是灰的
  mk({id:110, tx_date:"2026-10-27", direction:"expense", cat_parent:"交通", category:"打车",
      amount_cents:670, reimbursed:1, reimb_cents:670, reimb_date:"2026-10-28", travel:1,
      owner_id:3, owner_name:"李四", note:"去公司打车钱", remain_cents:0}),
  // 兜底分类「其他支出 / 其他」：标题只写一级，靠备注说清是什么（明细行不显示「其他 / 其他」）
  mk({id:111, tx_date:"2026-10-25", direction:"expense", cat_parent:"其他支出", category:"其他",
      amount_cents:2000, owner_id:3, owner_name:"李四", note:"公益捐款", remain_cents:2000}),
  // 报销到账：分类从原单继承（交通 / 打车），标题必须写「报销到账」，原单信息放第二行
  mk({id:112, tx_date:"2026-10-25", direction:"refund", offset_kind:"reimb",
      cat_parent:"交通", category:"打车", amount_cents:3000, owner_id:3, owner_name:"李四",
      note:"9 月差旅报销", parent_id:103, parent_date:"2026-10-18",
      parent_cat_parent:"交通", parent_category:"打车", parent_amount:3000, remain_cents:3000}),
];
const MOCK_STATS = {
  month:"2026-10", expense:81680, expense_gross:89560, refund:7880,
  refund_offset:5000, refund_orphan:2880, income:1500000,
  shared:4560, pending:3000, pending_cnt:1, voucher:12000, voucher_cnt:1, reimb_done:0,
  by_category:[{cat_parent:"购物",total:72000},{cat_parent:"餐饮",total:9560},{cat_parent:"交通",total:3000}],
  by_owner:[{name:"张三",id:2,expense:50000},{name:"李四",id:3,expense:31680}],
};
// 分类管理面板的编辑源：{方向: {大类: {color_slot, subs}}}
const MOCK_CAT_CFG = (() => {
  const seed = {expense:{"餐饮":1,"交通":2,"购物":4,"生活缴费":6,"医疗":3,"娱乐":5,"人情":7,"其他支出":9}};
  const out = {};
  for (const k in MOCK_CATS) {
    out[k] = {};
    for (const p in MOCK_CATS[k])
      out[k][p] = {color_slot: (seed[k] || {})[p] === undefined ? null : seed[k][p],
                   subs: MOCK_CATS[k][p].slice()};
  }
  return out;
})();
const MOCK_CAT_USAGE = {
  expense: {"餐饮":{total:186,month:23},"交通":{total:74,month:8},"购物":{total:52,month:11},
            "生活缴费":{total:38,month:5},"医疗":{total:6,month:0},"娱乐":{total:19,month:2},
            "人情":{total:9,month:1},"其他支出":{total:12,month:2}},
  income:  {"工资":{total:20,month:1},"补贴":{total:7,month:1}},
  refund:  {"退款":{total:15,month:2}},
};
const MOCK_CAT_LOGS = [
  {action:"rename", detail:"娱乐 / 旅行 → 娱乐 / 出游", cat_parent:"娱乐", affected:3,
   user_name:"张三", ts:"2026-10-26T21:14:02"},
  {action:"color", detail:"交通 换成色位 2", cat_parent:"交通", affected:0,
   user_name:"张三", ts:"2026-10-25T09:02:41"},
  {action:"add", detail:"新增大类 宠物", cat_parent:"宠物", affected:0,
   user_name:"张三", ts:"2026-10-20T18:33:10"},
];
const MOCK_LINKS = {
  offset: {id:106, tx_date:"2026-10-11", amount_cents:5000, offset_kind:"refund",
           note:"退一份", cat_parent:"餐饮", category:"早餐"},
  items: [
    {link_id:1, parent_id:105, alloc_cents:3000, tx_date:"2026-10-12", cat_parent:"餐饮",
     category:"早餐", note:"团购双人餐", parent_cents:10000, used_other:0, max_cents:10000},
    {link_id:2, parent_id:104, alloc_cents:2000, tx_date:"2026-10-15", cat_parent:"餐饮",
     category:"外卖", note:"周末外卖", parent_cents:4560, used_other:0, max_cents:4560},
  ],
  allocated: 5000, unallocated: 0,
};
window.fetch = (url) => {
  const u = String(url);
  const q = new URLSearchParams(u.split("?")[1] || "");
  let data = {};
  if (u.indexOf("/api/meta") === 0) data = {user: MOCK_ME, users: MOCK_USERS,
      categories: MOCK_CATS, ai_enabled: true};
  else if (/\/api\/tx\/\d+\/links/.test(u)) data = MOCK_LINKS;
  else if (u.indexOf("/api/me") === 0) data = {user: MOCK_ME};
  else if (u.indexOf("/api/settings") === 0) data = {ai_model: "glm-5.3-flash",
      ai_key_masked: "f94a12…9c3e", ai_enabled: true};
  else if (u.indexOf("/api/categories/config") === 0) data = {config: MOCK_CAT_CFG, usage: MOCK_CAT_USAGE};
  else if (u.indexOf("/api/categories/logs") === 0) data = {items: MOCK_CAT_LOGS};
  else if (u.indexOf("/api/ai/logs") === 0) data = {
    week: {count: 23, ok: 22, prompt_tokens: 31600, completion_tokens: 1580, avg_ms: 1680},
    items: [
      {kind: "image", status: "ok", input_brief: "超市小票", latency_ms: 4120, ts: "2026-10-27T17:21:03"},
      {kind: "text", status: "ok", input_brief: "午饭 25 共同", latency_ms: 1240, ts: "2026-10-27T12:33:41"},
      {kind: "text", status: "ok", input_brief: "打车去客户 32 待报销", latency_ms: 1380, ts: "2026-10-27T09:12:08"},
      {kind: "image", status: "error", input_brief: "小票", latency_ms: 45010,
       error: "AI 请求失败: timed out", ts: "2026-10-26T20:05:55"},
    ]};
  else if (u.indexOf("/api/stats") === 0) data = MOCK_STATS;
  else if (u.indexOf("/api/tx?") === 0 || u.indexOf("/api/tx") === 0) {
    let items = MOCK_TX.filter(t => !q.get("month") || t.tx_date.startsWith(q.get("month")));
    // 标记筛选与后端同一套语义：逗号分隔，mark 之间「或」，exclude 整体取反
    const MARK_TEST = {
      shared: t => !!t.shared, pending: t => !!t.reimb_pending, reimb: t => !!t.reimbursed,
      voucher: t => !!t.pending_use, travel: t => !!t.travel,
      refunded: t => (t.refunded_cents || 0) > 0,
    };
    const pick = v => (v || "").split(",").filter(k => MARK_TEST[k]);
    const wantMarks = pick(q.get("mark")), banMarks = pick(q.get("exclude"));
    if (wantMarks.length) items = items.filter(t => wantMarks.some(k => MARK_TEST[k](t)));
    if (banMarks.length) items = items.filter(t => !banMarks.some(k => MARK_TEST[k](t)));
    if (q.get("start")) items = items.filter(t => t.tx_date >= q.get("start"));
    if (q.get("end")) items = items.filter(t => t.tx_date <= q.get("end"));
    const kw = (q.get("q") || "").trim();      // 空格分词 + 金额双路，与后端同一套规则
    // 净额：支出扣掉分给它的冲抵，退款/收入取负值——排序和金额搜索都按它，与后端 NET_OUT_SQL 对齐
    const netOut = t => t.direction === "expense"
      ? (t.remain_cents == null ? t.amount_cents : t.remain_cents) : -t.amount_cents;
    if (kw) items = items.filter(t => kw.split(/\s+/).every(w => {
      const cents = /^\d+(\.\d{1,2})?$/.test(w) ? Math.round(parseFloat(w) * 100) : null;
      return (t.note || "").indexOf(w) >= 0 || (t.category || "").indexOf(w) >= 0
        || (t.cat_parent || "").indexOf(w) >= 0 || (t.owner_name || "").indexOf(w) >= 0
        || (cents !== null && (t.amount_cents === cents || netOut(t) === cents));
    }));
    const sort = q.get("sort") || "date_desc";   // 跟服务端白名单保持同一套键名
    items = items.slice().sort((a, b) =>
      sort === "amount_desc" ? netOut(b) - netOut(a) :
      sort === "amount_asc"  ? netOut(a) - netOut(b) :
      sort === "date_asc"    ? (a.tx_date < b.tx_date ? -1 : 1) :
                               (a.tx_date > b.tx_date ? -1 : 1));
    // summary 按筛选后的结果现算，别写死——否则「共找到 8 笔」下只列 1 条，看着像 bug
    // reimb 必须留着：前端「净 = 支出 − 退款 − 报销」，少这个字段整行就成 NaN
    const sum = items.reduce((s, t) => {
      s.count += 1;
      if (t.direction === "expense") s.expense += t.amount_cents;
      else if (t.direction === "income") s.income += t.amount_cents;
      else if (t.offset_kind === "reimb") s.reimb += t.amount_cents;
      else s.refund += t.amount_cents;
      return s;
    }, {count: 0, expense: 0, income: 0, refund: 0, reimb: 0});
    data = {items, summary: sum};
  }
  else if (u.indexOf("/api/refund-targets") === 0) {
    // 报销和退款是两条路：报销只列标了「待报销」的、不按金额筛（一次到账摊多笔）
    data = u.indexOf("kind=reimb") > 0
      ? {auto: null, mode: "reimb", relaxed: false, items: [
          {id:102, tx_date:"2026-10-20", cat_parent:"购物", category:"数码", note:"高铁票",
           amount_cents:60000, remain_cents:60000, diff_cents:60000, match:"reimb",
           owner_name:"张三"},
          {id:104, tx_date:"2026-10-18", cat_parent:"餐饮", category:"外卖", note:"住宿",
           amount_cents:4560, remain_cents:4560, diff_cents:4560, match:"reimb",
           owner_name:"张三"},
          {id:109, tx_date:"2026-10-04", cat_parent:"交通", category:"公交地铁", note:"打车",
           amount_cents:200, remain_cents:200, diff_cents:200, match:"reimb",
           owner_name:"李四"}]}
      : {auto: 102, items: [
          {id:102, tx_date:"2026-10-20", cat_parent:"购物", category:"数码", note:"手机壳",
           amount_cents:60000, remain_cents:60000, diff_cents:0, match:"same"},
          {id:104, tx_date:"2026-10-18", cat_parent:"餐饮", category:"外卖", note:"外卖",
           amount_cents:4560, remain_cents:4560, diff_cents:1560, match:"near"}],
         window_days: 30, tolerance_cents: 2500};
  }
  else if (u.indexOf("/api/refunds") === 0) data = {items:[
    {id:106, tx_date:"2026-10-11", amount_cents:5000, owner_name:"张三", note:"退一份"}]};
  else if (u.indexOf("/api/loans") === 0) data = {
    items: [
      {id:1, direction:"out", counterparty:"朋友", amount_cents:10000000, repaid_cents:0,
       loan_date:"2026-10-12", note:"给朋友借款", owner_id:3, owner_name:"李四",
       settled_at:null, remain_cents:10000000, settled:false},
      {id:2, direction:"in", counterparty:"同事", amount_cents:500000, repaid_cents:200000,
       loan_date:"2026-09-20", note:"", owner_id:2, owner_name:"张三",
       settled_at:null, remain_cents:300000, settled:false}],
    summary: {out_open:10000000, in_open:300000, open_count:2}};
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(data)});
};
</script>
"""

# 每个场景：注入到页面末尾的动作
SCENES = {
    "list":     'state.tab = "list"; renderNav(); render();',
    "filter-sheet": 'state.tab = "list"; renderNav(); render();'
                    'setTimeout(() => { openFilter(); const q = $("#f-q");'
                    ' q.value = "外卖 李四"; q.dispatchEvent(new Event("input")); }, 200);',
    "search-result": 'state.filter = { ...state.filter, q: "外卖 李四",'
                     ' scope: "range", start: "2026-09-01", end: "2026-10-27" };'
                     ' state.tab = "list"; renderNav(); render();',
    "list-amount": 'state.sort = "amount_desc"; state.tab = "list"; renderNav(); render();',
    # 反选：近30天 + 支出 + 旅行，但排除已报销 / 有退款
    "filter-reverse": 'state.filter = { ...EMPTY_FILTER, direction: "expense",'
                      ' scope: "range", start: "2026-09-30", end: "2026-10-29",'
                      ' mark: "travel", exclude: "reimb,refunded" };'
                      'state.tab = "list"; renderNav(); render();'
                      'setTimeout(() => openFilter(), 400);',
    "list-reverse": 'state.filter = { ...EMPTY_FILTER, direction: "expense",'
                    ' scope: "range", start: "2026-09-30", end: "2026-10-29",'
                    ' mark: "travel", exclude: "reimb,refunded" };'
                    'state.tab = "list"; renderNav(); render();',
    "sort-sheet": 'state.tab = "list"; renderNav(); render();'
                  'setTimeout(() => openSort(), 200);',
    "stats":    'state.tab = "stats"; renderNav(); render();',
    "stats-goto": 'state.tab = "stats"; renderNav(); render();'
                  'setTimeout(() => { const b = document.querySelector(\'[data-goto="pending"]\');'
                  ' if (b) b.click(); }, 500);',
    "add-refund": '''state.form.direction = "refund"; state.form.cat_parent = "退款";
        state.form.category = "购物退款"; state.amountText = "400.00"; renderAdd();''',
    "add-expense": 'renderAdd();',
    "add-income": 'setTimeout(() => { state.form.direction = "income";'
                  ' state.form.cat_parent = "补贴"; state.form.category = "出差补贴";'
                  ' state.amountText = "600.00"; renderAdd(); }, 1200);',
    "add-reimb": 'setTimeout(() => { state.form.direction = "refund";'
                 ' state.offsetKind = "reimb"; state.amountText = "6.70";'
                 ' renderAdd(); }, 1200);',
    # 一次到账摊多笔：差补按「到账 − 已选合计」自动算出（1000 − 647.60 = 352.40）
    "add-reimb-multi": 'setTimeout(() => { state.form.direction = "refund";'
                       ' state.offsetKind = "reimb"; state.amountText = "1000";'
                       ' state.subsidyCat = "出差补贴";'
                       ' renderAdd(); }, 1200);',
    # 刚切到「报销到账」、金额还没填：候选应是「选它」而不是「已选」
    "add-reimb-empty": 'setTimeout(() => { state.form.direction = "refund";'
                       ' state.offsetKind = "reimb"; state.amountText = "";'
                       ' renderAdd(); }, 1200);',
    # 手动改过差补（300）后不再被自动覆盖，hint 里给「改回自动」
    "add-reimb-manual": 'setTimeout(() => { state.form.direction = "refund";'
                        ' state.offsetKind = "reimb"; state.amountText = "1000";'
                        ' state.subsidyManual = true; state.subsidyText = "300";'
                        ' state.subsidyCat = "出差补贴";'
                        ' renderAdd(); }, 1200);',
    # 报销候选的默认勾选自测：金额没填不该预选，填够才全选，手动动过就不再自动
    "reimb-selftest": """state.form.direction = "refund"; state.offsetKind = "reimb";
        state.amountText = ""; renderAdd();
        const log = [];
        const amtEl = () => document.querySelector("#amount");
        const selN = () => state.reimbSel.length;
        const type = v => { amtEl().value = v;
                            amtEl().dispatchEvent(new Event("input", {bubbles:true})); };
        setTimeout(() => {
          log.push("空=" + selN());
          type("700");  log.push("700元=" + selN());
          type("100");  log.push("100元=" + selN());
          type("700");  log.push("回到700=" + selN());
          document.querySelector("[data-rt]").click();
          log.push("手点1笔=" + selN() + "/touched=" + state.reimbTouched);
          type("100");  log.push("改小后仍=" + selN());
          type("700");  log.push("改大后仍=" + selN());
          document.title = log.join(" || ");
        }, 1400);""",
    "voucher":  'state.tab = "list"; renderNav(); render();'
                'setTimeout(() => openVoucher(MOCK_TX.find(t => t.id === 101)), 200);',
    "refund-sheet": 'state.tab = "list"; renderNav(); render();'
                    'setTimeout(() => openRefund(MOCK_TX.find(t => t.id === 102)), 200);',
    "edit-refund": 'state.tab = "list"; renderNav(); render();'
                   'setTimeout(() => openEdit(107, MOCK_TX), 400);',
    # 挂了原单的冲抵记录：方向 / 分类整段只读（服务端会按原单覆盖，给下拉就是骗人）
    "edit-refund-linked": 'state.tab = "list"; renderNav(); render();'
                          'setTimeout(() => openEdit(106, MOCK_TX), 400);',
    "edit-reimb-linked": 'state.tab = "list"; renderNav(); render();'
                         'setTimeout(() => openEdit(112, MOCK_TX), 400);',
    # 只读化自测：挂原单的三项不该有下拉；无主退款仍要能选分类
    "edit-ro-selftest": """state.tab = "list"; renderNav(); render();
        const has = id => !!document.getElementById(id);
        setTimeout(() => {
          const log = [];
          openEdit(106, MOCK_TX);
          log.push("linked=" + ["e-dir","e-parent","e-cat"].map(has).join(","));
          log.push("ro=" + document.querySelectorAll("#sheet .ro").length);
          log.push("tags=" + [...document.querySelectorAll("#sheet .ro-tag")]
                             .map(e => e.textContent.trim()).join("|"));
          log.push("jump=" + has("e-go-parent"));
          openEdit(107, MOCK_TX);
          log.push("orphan=" + ["e-dir","e-parent","e-cat"].map(has).join(","));
          log.push("orphan_ro=" + document.querySelectorAll("#sheet .ro").length);
          document.title = log.join(" || ");
        }, 700);""",
    "edit-expense": 'state.tab = "list"; renderNav(); render();'
                    'setTimeout(() => openEdit(102, MOCK_TX), 400);',
    "edit-locked": 'state.tab = "list"; renderNav(); render();'
                   'setTimeout(() => openEdit(110, MOCK_TX), 400);',
    "alloc": 'state.tab = "list"; renderNav(); render();'
             'setTimeout(() => openLinks(MOCK_TX.find(t => t.id === 106)), 400);',
    "edit-refunded": 'state.tab = "list"; renderNav(); render();'
                     'setTimeout(() => openEdit(105, MOCK_TX), 200);',
    "loans":    'state.tab = "list"; renderNav(); render();'
                'setTimeout(() => { const b = $("#loan-toggle"); if (b) b.click(); }, 200);',
    "loan-sheet": 'state.tab = "list"; renderNav(); render();'
                  'setTimeout(() => { loadLoans().then(() => openLoanSheet(loansCache[0])); }, 200);',
    "add-photo": '''state.amountText = "25.00";
        state.form.cat_parent = "餐饮"; state.form.category = "午餐";
        state.aiText = "公司楼下超市";
        state.aiHint = "微信支付 25.00 元 收款方：全家便利店";
        state.aiImage = "data:image/svg+xml;utf8," + encodeURIComponent(
          '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="360">' +
          '<rect width="600" height="360" fill="#f2f2f0"/>' +
          '<text x="300" y="150" font-size="30" text-anchor="middle" fill="#8a8a8a">微信支付</text>' +
          '<text x="300" y="220" font-size="52" text-anchor="middle" fill="#1b1b1d">\\u00a525.00</text>' +
          '</svg>');
        renderAdd();''',
    "me-admin": 'state.me.is_admin = 1; state.tab = "me"; renderNav(); render();',
    # 分类管理：默认收起 → 点开「餐饮」看就地编辑态；再改个颜色让「未保存」浮条出来
    "cat-list": 'state.me.is_admin = 1; state.tab = "me"; renderNav(); render();'
                'setTimeout(() => { const c = $("#cat-editor").closest(".card"); c.scrollIntoView(); window.scrollBy(0, -92); }, 700);',
    "cat-detail": 'state.me.is_admin = 1; state.tab = "me"; renderNav(); render();'
                  'setTimeout(() => { const c = $("#cat-editor").closest(".card"); c.scrollIntoView(); window.scrollBy(0, -92);'
                  ' const hd = document.querySelector(".cat-hd"); if (hd) hd.click(); }, 700);',
    "cat-dirty": 'state.me.is_admin = 1; state.tab = "me"; renderNav(); render();'
                 'setTimeout(() => { const c = $("#cat-editor").closest(".card"); c.scrollIntoView(); window.scrollBy(0, -92);'
                 ' const hd = document.querySelector(".cat-hd"); if (hd) hd.click(); }, 700);'
                 'setTimeout(() => { const sw = document.querySelectorAll(".cat-sw")[2];'
                 ' if (sw) sw.click(); }, 1100);',
    # 只留分类管理这一张卡 + 弹出「未保存」条：验证浮条不会被底栏压住、也不挡内容
    "cat-selftest": """state.me.is_admin = 1; state.tab = "me"; renderNav(); render();
        const log = [];
        setTimeout(() => {
          const hd = document.querySelector(".cat-hd"); hd.click();
          log.push("expand=" + !!document.querySelector("#cat-pname"));
          const pn = document.querySelector("#cat-pname");
          pn.value = "伙食"; pn.dispatchEvent(new Event("change", {bubbles:true}));
          log.push("rename=" + catOpen + "/" + document.querySelector(".cat-hd .nm").textContent);
          const sn = document.querySelector("#cat-sub-new"); sn.value = "夜宵";
          sn.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", bubbles:true}));
          log.push("subs=" + catEdit.expense["伙食"].subs.join(","));
          log.push("focusBack=" + (document.activeElement.id || document.activeElement.tagName));
          const pn2 = document.querySelector("#cat-pname");
          pn2.value = "交通"; pn2.dispatchEvent(new Event("change", {bubbles:true}));
          log.push("dup=" + ((document.querySelector(".cat-err")||{}).textContent||"") + "|val=" + document.querySelector("#cat-pname").value);
          log.push("diff=" + catDiffCount() + "|bar=" + document.querySelector(".cat-bar").className);
          const dp = document.querySelector(".cat-delp"); dp.click();
          log.push("armed=" + document.querySelector(".cat-delp").textContent.indexOf("再点一次"));
          document.querySelector(".cat-delp").click();
          log.push("deleted=" + (catEdit.expense["伙食"] === undefined));
          document.title = log.join(" || ");
        }, 900);""",
    "cat-bar": 'state.me.is_admin = 1; state.tab = "me"; renderNav(); render();'
               'setTimeout(() => { document.querySelectorAll("#view > .card").forEach((c, i) => {'
               ' if (i !== 2) c.remove(); }); const hd = document.querySelector(".cat-hd");'
               ' if (hd) hd.click(); }, 700);'
               'setTimeout(() => { const sw = document.querySelectorAll(".cat-sw")[2];'
               ' if (sw) sw.click(); }, 1100);',
    # 长按拖拽排序自测：先拖子类，再拖大类，把结果与「未保存计数」写进 title。
    # 指针事件得手工派发——无头浏览器里没有真实手指，只能验证逻辑，手感仍需真机
    "cat-drag-selftest": """state.me.is_admin = 1; state.tab = "me"; renderNav(); render();
        const log = [];
        const pt = (type, el, x, y, id) => (el || window).dispatchEvent(new PointerEvent(type, {
          bubbles: true, cancelable: true, pointerId: id || 7, pointerType: "touch",
          isPrimary: true, clientX: x, clientY: y }));
        const hold = (el, x, y) => pt("pointerdown", el, x, y);
        setTimeout(() => {
          document.querySelector(".cat-hd").click();               // 展开第一个大类
          log.push("open=" + (catOpen || "-"));
          log.push("subs0=" + catEdit.expense[catOpen].subs.join(","));
          const chips = [...document.querySelectorAll(".cat-subs .cat-sub")];
          const a = chips[0], c = chips[3];
          const ra = a.getBoundingClientRect(), rc = c.getBoundingClientRect();
          hold(a, ra.left + 8, ra.top + 8);
          setTimeout(() => {                                        // 超过长按时长 → 进入拖动
            pt("pointermove", window, rc.left + rc.width - 2, rc.top + rc.height / 2);
            pt("pointermove", window, rc.left + rc.width - 2, rc.top + rc.height - 2);
            pt("pointerup", window, rc.left + rc.width - 2, rc.top + rc.height - 2);
            setTimeout(() => {
              log.push("subsAfter=" + catEdit.expense[catOpen].subs.join(","));
              log.push("subDragDone=" + !document.querySelector(".dragging")
                       + "|ph=" + document.querySelectorAll(".cat-ph-sub,.cat-ph").length);
              const names = Object.keys(catEdit.expense);
              const first = document.querySelectorAll(".cat-item")[0];
              const third = document.querySelectorAll(".cat-item")[2];
              const r1 = first.querySelector(".cat-hd").getBoundingClientRect();
              const r3 = third.getBoundingClientRect();
              hold(first.querySelector(".cat-hd"), r1.left + 40, r1.top + 12);
              setTimeout(() => {
                pt("pointermove", window, r3.left + 40, r3.top + r3.height / 2 + 4);
                pt("pointermove", window, r3.left + 40, r3.top + r3.height - 6);
                pt("pointerup", window, r3.left + 40, r3.top + r3.height - 6);
                setTimeout(() => {
                  log.push("parentsBefore=" + names.join(","));
                  log.push("parentsAfter=" + Object.keys(catEdit.expense).join(","));
                  log.push("moved=" + names[0] + "→" + Object.keys(catEdit.expense)[2]);
                  log.push("diff=" + catDiffCount());
                  log.push("bar=" + document.querySelector(".cat-bar").className);
                  log.push("openKept=" + (catOpen || "-"));
                  document.title = log.join(" || ");
                }, 120);
              }, 420);
            }, 120);
          }, 420);
        }, 900);""",
    # 拖动进行中的样子（不松手）：截图用，肉眼确认浮动块跟手、虚线占位在目标位置
    "cat-drag-shot": """state.me.is_admin = 1; state.tab = "me"; renderNav(); render();
        const pt = (type, el, x, y) => (el || window).dispatchEvent(new PointerEvent(type, {
          bubbles: true, cancelable: true, pointerId: 5, pointerType: "touch",
          isPrimary: true, clientX: x, clientY: y }));
        setTimeout(() => {
          const c = $("#cat-editor").closest(".card");
          c.scrollIntoView(); window.scrollBy(0, -92);
          const hd = document.querySelector(".cat-hd");
          const r = hd.getBoundingClientRect();
          pt("pointerdown", hd, r.left + 40, r.top + 12);
          setTimeout(() => {
            const third = document.querySelectorAll(".cat-item")[2];
            const r3 = third.getBoundingClientRect();
            pt("pointermove", window, r3.left + 40, r3.top + r3.height - 8);
          }, 420);
        }, 900);""",
    "cat-drag-cancel": """state.me.is_admin = 1; state.tab = "me"; renderNav(); render();
        const log = [];
        const pt = (type, el, x, y) => (el || window).dispatchEvent(new PointerEvent(type, {
          bubbles: true, cancelable: true, pointerId: 9, pointerType: "touch",
          isPrimary: true, clientX: x, clientY: y }));
        setTimeout(() => {
          const hd = document.querySelector(".cat-hd");
          const r = hd.getBoundingClientRect();
          // ① 短按 120ms 就松手 → 应该当成点击，展开
          pt("pointerdown", hd, r.left + 40, r.top + 12);
          setTimeout(() => {
            pt("pointerup", window, r.left + 40, r.top + 12);
            hd.click();
            log.push("tapOpened=" + (catOpen || "-"));
            log.push("diffAfterTap=" + catDiffCount());
            // ② 按住 80ms 后大幅滑动（早于长按时长）→ 视为滚动，顺序不变
            const before = Object.keys(catEdit.expense).join(",");
            const item = document.querySelectorAll(".cat-item")[0];
            const r2 = item.querySelector(".cat-hd").getBoundingClientRect();
            pt("pointerdown", item.querySelector(".cat-hd"), r2.left + 40, r2.top + 12);
            setTimeout(() => {
              pt("pointermove", window, r2.left + 40, r2.top + 90);
              log.push("draggingAfterSlide=" + document.querySelectorAll(".dragging").length);
              pt("pointerup", window, r2.left + 40, r2.top + 90);
              log.push("orderKept=" + (before === Object.keys(catEdit.expense).join(",")));
              log.push("diffAfterSlide=" + catDiffCount());
              // ③ 原地拖、原地松：不该算改动
              const it = document.querySelectorAll(".cat-item")[1];
              const r3 = it.getBoundingClientRect();
              pt("pointerdown", it.querySelector(".cat-hd"), r3.left + 40, r3.top + 10);
              setTimeout(() => {
                pt("pointermove", window, r3.left + 40, r3.top + 14);
                pt("pointerup", window, r3.left + 40, r3.top + 14);
                log.push("diffAfterNoop=" + catDiffCount());
                log.push("orderAfterNoop=" + Object.keys(catEdit.expense).join(","));
                document.title = log.join(" || ");
              }, 420);
            }, 80);
          }, 120);
        }, 900);""",
}

BASE = (HERE / "www" / "index.html").read_text(encoding="utf-8")
assert "\n<script>\n" in BASE, "index.html 结构变了，找不到主脚本"
# 无头 Edge 最小窗口宽度约 476px，fixed 元素会按视口走，强制收到 390 才和手机一致
CSS_FIX = ("<style>*,*::before,*::after{transition:none!important;animation:none!important}"
           "html,body{width:390px!important;margin:0}"
           "nav,.sheet{max-width:390px!important;margin:0!important}</style>")

for name, action in SCENES.items():
    html = BASE.replace("\n<script>\n", CSS_FIX + MOCK + "\n<script>\n", 1)
    # 预览数据钉在 MOCK_STATS.month（2026-10）。不把 state.month 也钉住，
    # 一过月份头一天，页面就打开「当月」而数据在 10 月 —— 每张列表截图都是空的。
    html = html.replace("</body>",
                        f"<script>setTimeout(() => {{ state.month = MOCK_STATS.month; {action} }}, 300);</script></body>",
                        1)
    (OUT / f"{name}.html").write_text(html, encoding="utf-8")
    print("写出", name)

if "--no-shot" in sys.argv:
    sys.exit(0)

# --only=a,b 只重截这几个场景：改一处 UI 时不必把二十多张图全刷一遍
ONLY = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), "")
for name in (ONLY.split(",") if ONLY else SCENES):
    png = OUT / f"{name}.png"
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2", "--window-size=420,1500",
                    "--virtual-time-budget=2500", f"--screenshot={png}",
                    (OUT / f"{name}.html").as_uri()], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("截图", png.name, png.exists())
