# open-ledger · 家庭 / 小团队账本

[![tests](https://github.com/rain-Qing/open-ledger/actions/workflows/tests.yml/badge.svg)](https://github.com/rain-Qing/open-ledger/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

一个面向家庭与小团队的自托管账本，支持支出 / 收入 / 退款、冲抵与报销、净额统计、CSV 导出和可选的 AI 记账。

- **单文件后端**：`ledger/server.py`，Python 3.11+ 标准库 + SQLite，无第三方依赖
- **单页前端**：`ledger/www/index.html`，原生 JS，无框架、无构建步骤
- **可重建**：一条命令部署，数据只有一个 SQLite 文件
- **有回归**：157 个用例 / 972 条断言，全部跑在临时库上

> 本仓库从私有站点仓库拆出，作为账本功能的独立源码与发版地。旧提交历史保留在私有仓库，未带入本仓库。

## 功能

| 模块 | 能力 |
|---|---|
| 记一笔 | 支出 / 收入 / 退款；金额、日期、分类、归属、备注；共同支出、待报销、已报销、待用券、旅行五个标记 |
| 明细 | 月份 / 日期区间 / 归属 / 方向 / 标记 / 分类 / 金额区间 / 关键词筛选；四种排序；CSV 导出 |
| 统计 | 净支出、原额支出、收入、结余、分类饼图、归属统计、待报销/待用券累计 |
| 退款 / 报销 | 退款候选匹配；一条退款冲抵多笔支出；报销到账 + 差补拆分；无主退款/报销 |
| 分类 | 管理员在线增删改、拖动排序、改色；有历史账单的分类禁止删除 |
| AI | 一句话或支付截图 → 账单草稿；智谱 GLM，Key 可选，未配置时入口隐藏 |
| 借款 | 独立的借款台账，不进入收支统计 |
| PWA | 可添加到手机主屏幕；自毁 Service Worker 处理旧缓存 |

## 快速开始

```bash
git clone https://github.com/rain-Qing/open-ledger.git open-ledger
cd open-ledger

LEDGER_HOME="$PWD/ledger" LEDGER_PORT=9103 python3 ledger/server.py
```

打开 <http://127.0.0.1:9103>。

首次启动会自动建库并创建管理员 `admin`，**初始密码只打印一次到启动日志**：

```text
初次启动 管理员账号 admin / xxxxxxxx
```

也可以在首次启动前指定：

```bash
LEDGER_ADMIN_PASSWORD='your-strong-password' \
LEDGER_HOME="$PWD/ledger" LEDGER_PORT=9103 python3 ledger/server.py
```

数据默认在 `ledger/data/ledger.db`。

## 部署到服务器

运行时目录约定为 `/opt/ledger`（systemd 单元里写死了这个路径）：

```bash
sudo mkdir -p /opt/ledger
sudo ./deploy/deploy.sh --dry-run   # 先看会改什么
sudo ./deploy/deploy.sh             # 同步代码 + systemd + 备份 cron + 健康检查
```

`deploy/deploy.sh` 会：

1. 同步 `server.py`、`www/`、备份脚本、systemd 单元与 drop-in；
2. 安装 `/etc/cron.d/ledger-backup`（每天 04:35）；
3. 只在文件变化时 `systemctl restart ledger`；
4. 轮询 `http://127.0.0.1:9103/healthz`，失败打印回滚命令。

目标机已有 `/etc/systemd/system/ledger.service.d/memory.conf` 时会被保留，不会被仓库里的 96M 基线覆盖。

反向代理示例见 [`deploy/caddy/jizhang.caddy.example`](deploy/caddy/jizhang.caddy.example)。把域名换成你自己的，再 `caddy validate` + `systemctl reload caddy`。

> 生产环境请放在 HTTPS 反代之后。服务本身只监听 `127.0.0.1:9103`。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LEDGER_HOME` | `/opt/ledger` | 运行时根目录，包含 `data/` 与 `www/` |
| `LEDGER_PORT` | `9103` | 监听端口，只应绑本机 |
| `LEDGER_ADMIN_PASSWORD` | 随机生成 | 仅首次建库时使用 |
| `LEDGER_AI_MOCK` | 未设置 | 设为 `1` 时不发真实 AI 请求，仅供测试 |

AI Key 与模型保存在数据库 `settings` 表，管理员在「设置」页填写；仓库里没有任何密钥。

## 数据与备份

- 数据库：`ledger/data/ledger.db`，SQLite WAL 模式；
- 备份脚本：`ledger/ledger-backup.sh`，用 SQLite 在线 backup API 取一致快照，产物在 `/opt/backups`，保留 14 天；
- **不要用 `cp` / `tar` 直接拷运行中的 `ledger.db`**：WAL 里可能有未回写的数据，裸拷会静默丢账。

恢复时先停服务，再用备份包里的 `ledger.db` 覆盖，最后启动并检查 `/healthz`。

## 测试

```bash
python tests/ledger_suite.py
```

- 每个用例在临时目录建独立库、随机端口、独立进程，**不会打开生产库**；
- 默认报告写到 `docs/家庭账本-测试报告.md`（已 gitignore）；
- 只想跑一组：`python tests/ledger_suite.py --only 退款`；
- 测线上部署的那份代码：`python tests/ledger_suite.py --src /opt/ledger`。

历史缺陷探针：

```bash
python tests/probe_regressions.py
```

界面预览（可选，Windows + Edge）：`python ledger/_preview.py`，生成截图到 `ledger/_preview/`。

## 安全边界

这是一个**单租户、家庭/小团队规模**的应用，不是多租户/企业级系统：

- 任何登录账号都可以修改或删除其他人的记录（设计取舍）；
- 没有 CSRF token（依赖 SameSite=Lax Cookie 和 JSON 请求体）、没有 2FA、没有记录级权限；
- 登录失败限速是进程内内存计数，重启会清零；
- 部分写路径没有幂等键，超时重试可能重复（退款/冲抵分配/借款）；
- AI 是可选的外部服务，调用会产生费用，默认每人每小时 60 次上限。

部署时至少做到：HTTPS、强管理员密码、只监听本机、定期备份。

## 目录

```text
ledger/
  server.py              后端（标准库 HTTP + SQLite）
  www/                   单页前端与图标
  ledger-backup.sh       SQLite 在线备份
  _preview.py            界面预览（开发用，不参与部署）
tests/
  ledger_suite.py        回归套件
  probe_regressions.py   历史缺陷探针
deploy/
  deploy.sh              运行时代码同步 + 健康检查
  systemd/               ledger.service 与 96M 内存上限 drop-in
  cron/ledger-backup     每日备份
  caddy/                 反代示例
docs/
  家庭账本-功能大全.md    功能、接口、数据模型、统计口径
tools/
  repair-ledger-20261030.py  存量脏数据体检/修复（默认 dry-run）
```

## 文档

- [家庭账本-功能大全](docs/家庭账本-功能大全.md)：功能、API、数据模型、统计公式、扩展指南
- [AGENTS.md](AGENTS.md)：开发规范（测试、部署、禁改清单、踩过的坑）
- [CHANGELOG.md](CHANGELOG.md)：变更记录
- [ROADMAP.md](ROADMAP.md)：公开发布后的优化计划与优先级

## License

[MIT](LICENSE)
