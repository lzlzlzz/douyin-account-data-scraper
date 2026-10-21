#!/bin/bash
# 立即弹出扫码窗口；遇风控空白自动 5 分钟后重试（最多 3 轮）
cd "$(dirname "$0")"
PY="${PYTHON:-python}"
for i in 1 2 3; do
  echo "=== $(date '+%H:%M:%S') 尝试 $i：弹出 Chrome 扫码窗口 ==="
  "$PY" douyin_qr_login.py --timeout 900 --remark "pop_try$i" 2>&1 | tee "output/qr_login/pop_try$i.log"
  rc=${PIPESTATUS[0]}
  echo "=== $(date '+%H:%M:%S') 尝试 $i 结束 rc=$rc ==="
  if [ "$rc" = "0" ]; then
    echo ">>> 扫码成功，cookie 已保存"
    break
  fi
  if [ "$rc" = "2" ]; then
    echo ">>> 二维码未渲染（风控空白），5 分钟后自动重试"
    sleep 300
  else
    break
  fi
done
echo "=== $(date '+%H:%M:%S') pop_now 流程结束 ==="
