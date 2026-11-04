# 安全策略

## 支持版本

| 版本 | 是否接收安全修复 |
|---|---|
| `2.x` | ✅ |
| `< 2.0` | ❌ |

## 报告漏洞

**不要开公开 Issue。** 请任选其一：

1. GitHub 私密报告：<https://github.com/rain-Qing/open-ledger/security/advisories/new>
2. 邮件：`iyuqing@vip.qq.com`，标题以 `[open-ledger]` 开头

请尽量包含：影响版本、复现步骤、影响面（能读到 / 改到什么）、PoC 或请求样例。
这是个人维护项目：会在 7 天内确认，修复后与你协商披露时间，不提供赏金。

## 在范围内

- `ledger/server.py`：认证、会话、SQL 拼接、静态文件、CSV 导出、AI 接口
- `ledger/www/index.html`：XSS、点击劫持、敏感信息落屏
- `deploy/`：systemd 单元权限、备份脚本、部署脚本的越权写入

## 不在范围内

- README「安全边界」已明确列出的设计取舍：单租户、无记录级权限、无 CSRF token、无 2FA、限速内存计数
- 部署方自己的 HTTPS、防火墙、反向代理与备份策略
- 第三方 AI 服务（智谱 GLM）自身的问题
- 通过物理访问、root 权限或已泄露的服务器凭据发起的攻击

## 安全模型

服务默认只监听 `127.0.0.1`，**必须放在 HTTPS 反向代理之后**。会话 Cookie 带 `Secure`，
因此通过明文 HTTP（`localhost` 除外）访问会无法登录，这是预期行为。
