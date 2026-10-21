#!/bin/bash
# 冷却 15 分钟后自动重试扫码登录；风控未解除则隔 5 分钟再试一次
cd "$(dirname "$0")"
PY="${PYTHON:-python}"
echo "=== 冷却中（15分钟），预计 $(date -d '+15 minutes' '+%H:%M') 弹窗 ===" 
sleep 900
echo "=== $(date '+%H:%M:%S') 尝试1 ==="
"$PY" douyin_qr_login.py --timeout 900 --remark auto_retry1 2>&1 | tee output/qr_login/auto_retry1.log
rc=${PIPESTATUS[0]}
if [ "$rc" = "2" ]; then
  echo "=== $(date '+%H:%M:%S') 风控未解除，隔5分钟再试 ==="
  sleep 300
  echo "=== $(date '+%H:%M:%S') 尝试2 ==="
  "$PY" douyin_qr_login.py --timeout 900 --remark auto_retry2 2>&1 | tee output/qr_login/auto_retry2.log
fi
echo "=== $(date '+%H:%M:%S') 自动重试流程结束 ==="
