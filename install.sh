#!/bin/zsh
# 豆包自动签到 · macOS launchd 定时任务安装/卸载/测试
# install.sh 会自动探测 python3 和脚本目录, 从 plist.template 生成真实配置
# 用法: ./install.sh install | uninstall | test

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.doubao-auto-checkin"
PLIST_NAME="${LABEL}.plist"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"
TEMPLATE="$SCRIPT_DIR/plist.template"
HOUR=${CHECKIN_HOUR:-9}
MINUTE=${CHECKIN_MINUTE:-20}
POLL_SECONDS=${CHECKIN_POLL_SECONDS:-14400}

find_python3() {
  for cand in "$(which python3)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$cand" ] || continue
    if "$cand" -c "import playwright, Crypto" >/dev/null 2>&1; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

case "$1" in
  install)
    PY="$(find_python3)" || {
      echo "错误: 没找到同时装有 playwright 和 pycryptodome 的 python3"
      echo "先执行: pip3 install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright pycryptodome"
      exit 1
    }
    echo "使用 Python: $PY"
    mkdir -p "$HOME/Library/LaunchAgents" "$SCRIPT_DIR/data"
    sed -e "s|__LABEL__|$LABEL|g" \
        -e "s|__PYTHON3__|$PY|g" \
        -e "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" \
        -e "s|__HOUR__|$HOUR|g" \
        -e "s|__MINUTE__|$MINUTE|g" \
        -e "s|__POLL__|$POLL_SECONDS|g" \
        "$TEMPLATE" > "$DEST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$DEST"
    echo "已安装: 每天 ${HOUR}:${MINUTE} 签到 + 每 $((POLL_SECONDS/3600)) 小时兜底轮询 + 开机补跑（均幂等）"
    echo "日志: $SCRIPT_DIR/data/launchd.log"
    echo "查看状态: launchctl print gui/$(id -u)/$LABEL | head -20"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$DEST"
    echo "已卸载定时任务"
    ;;
  test)
    echo "手动跑一次签到测试..."
    PY="$(find_python3)" || PY="python3"
    "$PY" "$SCRIPT_DIR/checkin.py" run
    ;;
  *)
    echo "用法: $0 {install|uninstall|test}"
    exit 1
    ;;
esac
