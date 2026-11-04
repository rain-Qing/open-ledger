#!/bin/bash
# 家庭账本部署：仓库 → /opt/ledger 运行时，同步 systemd/备份 cron，然后重启 + 健康检查
#
# 用法：
#   ./deploy/deploy.sh              同步并重启（有变化时）
#   ./deploy/deploy.sh --dry-run    只看会改什么
#
# 约定：
#   · 运行时目录固定 /opt/ledger（systemd 单元里写死）
#   · data/ 是唯一真实账目，脚本只保证目录存在，绝不覆盖
#   · 每个文件覆盖前备份为 *.bak.<时间戳>，健康检查失败会打印回滚命令
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/ledger
TS=$(date +%Y%m%d-%H%M%S)
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }

CHANGED=0

put() {
  local src=$1 dst=$2 mode=$3
  [[ -f "$src" ]] || { bad "找不到 $src"; exit 1; }
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    return 0
  fi
  CHANGED=1
  if [[ $DRY -eq 1 ]]; then
    echo "  would update $dst"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  [[ -f "$dst" ]] && cp -a "$dst" "$dst.bak.$TS"
  install -m "$mode" "$src" "$dst"
  ok "$dst"
}

say "仓库：$REPO"
[[ $DRY -eq 1 ]] && echo "（dry-run：不改动任何文件）"

say "账本 → $DEST"
CHANGED=0
put "$REPO/ledger/server.py" "$DEST/server.py" 755

for f in "$REPO"/ledger/www/*; do
  put "$f" "$DEST/www/$(basename "$f")" 644
done

put "$REPO/ledger/ledger-backup.sh" /usr/local/bin/ledger-backup.sh 755
put "$REPO/deploy/cron/ledger-backup" /etc/cron.d/ledger-backup 644
put "$REPO/deploy/systemd/ledger.service" /etc/systemd/system/ledger.service 644
if [[ -f /etc/systemd/system/ledger.service.d/memory.conf ]]; then
  echo "  保留现有 memory.conf（机器专属调优）"
else
  put "$REPO/deploy/systemd/ledger.service.d/memory.conf" \
      /etc/systemd/system/ledger.service.d/memory.conf 644
fi

[[ $DRY -eq 0 ]] && mkdir -p "$DEST/data"

if [[ $DRY -eq 1 ]]; then
  if [[ $CHANGED -eq 1 ]]; then
    say "dry-run 结束：真跑会重启 ledger"
  else
    say "dry-run 结束：没有变化"
  fi
  exit 0
fi

if [[ $CHANGED -eq 1 ]]; then
  systemctl daemon-reload
  systemctl enable ledger >/dev/null 2>&1 || true
  systemctl restart ledger
  ok "systemctl restart ledger"
else
  echo "  无变化，不重启"
fi

say "健康检查"
CODE=""
for _ in $(seq 1 15); do
  CODE=$(curl -s -o /dev/null -m 2 -w '%{http_code}' http://127.0.0.1:9103/healthz || true)
  if [[ "$CODE" == "200" ]]; then
    ok "ledger 健康 (200)"
    say "完成 · 备份后缀 .bak.$TS"
    exit 0
  fi
  sleep 1
done

bad "健康检查失败（最后状态：${CODE:-无响应}）"
echo "回滚："
echo "    for f in \$(find $DEST -name '*.bak.$TS'); do"
echo "      install -m 755 \"\$f\" \"\${f%.bak.$TS}\"; done"
echo "    systemctl restart ledger"
exit 1
