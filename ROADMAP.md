# ROADMAP · 后续优化计划

> 记录 v2.0.0 公开发布后的优化项，按优先级排列。
> 完成一项就把 `[ ]` 改成 `[x]`，并在 [`CHANGELOG.md`](CHANGELOG.md) 记一笔。
> 基线：2026-11-04（公开仓库首日）。

## 当前状态

| 指标 | 值 |
|---|---|
| CI | Python 3.11 / 3.12 / 3.13 全绿 |
| 回归 | 157 用例 / 972 断言 + 10 条历史缺陷探针 |
| 版本 | tag `v2.0.0`；GitHub Release 尚未创建 |
| Topics | 12 / 20 |
| 社区健康度 | 57%（缺 CONTRIBUTING / CODE_OF_CONDUCT / Issue / PR 模板） |
| 运行时依赖 | 0（仅 Python 标准库 + SQLite） |

## 原则

- 运行时保持零第三方依赖、单文件后端、单页前端。
- 不做多租户与企业级权限；单租户是明确取舍，写进 README「安全边界」。
- 功能改动必须带回归用例（`tests/ledger_suite.py`），修缺陷优先补探针。
- 数据库变更必须写进 CHANGELOG「数据库」段，并保证旧库可自动迁移。

## P0 · 公开门面

- [ ] **创建 v2.0.0 Release**（S）
  tag 已存在，只缺 Release 说明。建完后首页会显示 Latest。
- [ ] **补齐社区文件**（M）
  `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`.github/ISSUE_TEMPLATE/{bug,feature}.yml`、`.github/pull_request_template.md`。
  验收：GitHub 社区健康度 ≥ 90%。
- [ ] **补 Topics**（S）
  现有 12 个，补 `sqlite`、`selfhosted`、`single-file`、`zero-dependencies`。
- [ ] **上传 Social preview**（S）
  1280×640，PNG ≤ 1 MB；仓库公开后才可上传。
- [ ] **在个人主页 Pin 仓库**（S）

## P1 · 安全与运维

- [ ] **systemd 改为非 root 运行**（M）
  `User=ledger` + `Group=ledger` + `StateDirectory=ledger`，收窄 `ReadWritePaths`；
  `deploy.sh` 负责创建用户与目录权限，并在 README 写明升级步骤。
  验收：服务以非 root 启动，账本能读写 `data/`，备份 cron 仍可执行。
- [ ] **写请求增加 Origin / Referer 校验**（S）
  不引入 CSRF token，只在 POST / PATCH / DELETE 上拒绝跨站 Origin。
  验收：同源请求正常；带外部 Origin 的写请求返回 403，并有用例覆盖。
- [ ] **明文 HTTP 的明确开关**（S）
  增加 `LEDGER_INSECURE_COOKIE=1`，用于局域网 HTTP 调试；默认仍为 Secure。
  验收：README 与 SECURITY 同步说明，不设置时行为不变。
- [ ] **补齐写路径幂等键**（M）
  退款、冲抵分配、借款三条路径接入 `client_token`（表结构与索引已支持）。
  验收：同键重放不产生重复记录，用例覆盖三条路径。
- [ ] **备份的异地与加密指引**（S）
  README 增加 rclone / age 示例，明确「同盘备份不等于灾备」。
- [ ] **AI 日志隐私说明**（S）
  README 与 SECURITY 注明 `ai_log` 保存输入前 120 字与草稿，90 天自动清理。

## P2 · 采用率

- [ ] **Dockerfile + docker-compose.yml**（M）
  基于 `python:3.13-slim`，非 root 用户，挂载 `/opt/ledger/data`，暴露 9103（仅 127.0.0.1）。
  验收：`docker compose up -d` 后 `/healthz` 返回 200，数据持久化在命名卷。
- [ ] **README 加界面截图 / GIF**（S）
  明细页与统计页各一张，展示退款冲抵与净额统计；图片放 `docs/images/`，控制在 500 KB 内。
- [ ] **英文 README**（M）
  `README.en.md`，顶部加中英切换；Description 与 Topics 已是英文，正文应跟上。
- [ ] **一键安装脚本**（M）
  `deploy/install.sh`：检查 Python 版本 → 创建目录与用户 → 安装 systemd 单元 → 启动并健康检查。
  与 `deploy/deploy.sh` 共用同一份路径变量，避免两套逻辑。
- [ ] **运行路径可配置**（M）
  `LEDGER_HOME` 已支持，但 `deploy.sh`、systemd 单元、备份脚本仍硬编码 `/opt/ledger` 与 `/opt/backups`；
  抽象为 `PREFIX` / `BACKUP_DIR`，默认值不变。

## P3 · 工程质量

- [ ] **CI 增加静态检查**（S）
  `python -m compileall -q ledger tools tests`，可选 `ruff check`（仅 CI 安装，不进入运行时）。
- [ ] **Python 版本前置检查**（S）
  启动时若低于 3.11，输出明确错误并退出，而不是抛语法错误。
- [ ] **拆分运维文档**（S）
  `docs/家庭账本-功能大全.md` 末尾的运维附录拆到 `docs/运维手册.md`，面向公开读者删去本机专属命令。
- [ ] **前端测试（暂缓）**（L）
  原生 JS + 无构建，引入 Playwright 成本较高；等前端逻辑继续增长再评估。

## P4 · 产品路线

- [ ] **CSV 导入**（L）
  目前只有导出。先做「按导出模板回填」的最小闭环：预校验 → 预览差异 → 单事务写入。
  验收：导出的 CSV 原样导入后账目一致；非法行给出逐行错误。
- [ ] **多账本**（L）
  现在 schema 是单账本 + 多账号。需要给 `transactions` / `loans` / `settings` 加 `book_id`，
  并评估所有统计、筛选、备份与前端状态。属于破坏性变更，必须单独发 MAJOR。
- [ ] **多用户权限细化**（L）
  当前所有成员权利对等（有意取舍）。若要做「只读成员 / 仅本人可改」，需要记录级权限模型，
  与「家庭账本」的定位需要先想清楚。
- [ ] **AI 接口可配置**（M）
  现在固定智谱 GLM。抽象 base_url / model / 鉴权头，支持 OpenAI 兼容端点；保留 mock 模式。
- [ ] **界面国际化**（L）
  先英文 README，再考虑 UI 文案抽取；账本术语（冲抵、差补）需要统一的英文对照表。

## 暂不做

以下需求明确不在近期范围，避免反复讨论：

- 2FA / SSO：单人自托管场景收益低，增加部署复杂度。
- 多租户 SaaS：与「数据只有一个 SQLite 文件」的定位冲突。
- 前端框架 / 构建链：保持单文件、原生 JS 是核心约束。
- 移动原生 App：PWA 已覆盖「加到主屏幕」场景。

## 维护方式

- 优先级只在有明确收益时调整，不按「听起来重要」排序。
- 每完成一项：勾选、写 CHANGELOG、如有行为变化同步 README / SECURITY。
- 新增优化项先追加到对应优先级末尾；跨优先级的大改动（如多账本）先开 Issue 讨论再动手。
