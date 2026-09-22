#!/usr/bin/env bash
# ==============================================================================
# AntMinerTab - 一键停止所有旧进程并释放端口脚本
# 适用环境: Ubuntu / Debian / Linux
# ==============================================================================

set -u

echo "=================================================================="
echo " 🛑 正在检查并停止所有 AntMiner 相关旧进程..."
echo "=================================================================="

# 1. 检查并停止 systemd 服务（如果是作为服务安装的）
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet antminer 2>/dev/null; then
        echo "[-] 检测到 systemd 守护服务 (antminer.service) 正在运行，正在停止..."
        sudo systemctl stop antminer || true
        echo "[✓] systemd 服务已停止"
    fi
fi

# 2. 终止各 Python 监控及 Web 进程 (antminer_web.py / antminer_monitor.py / antminer_gui.py)
TARGETS=("antminer_web.py" "antminer_monitor.py" "antminer_gui.py")

for target in "${TARGETS[@]}"; do
    PIDS=$(pgrep -f "$target" || true)
    if [ -n "$PIDS" ]; then
        echo "[-] 发现正在运行的 $target (PID: $PIDS)，正在发送 SIGTERM 优雅退出..."
        pkill -f "$target" || true
        sleep 1
        
        # 检查是否仍有残留，若有则强制杀死
        REMAINING=$(pgrep -f "$target" || true)
        if [ -n "$REMAINING" ]; then
            echo "[!] 进程仍未退出，正在强制杀除 (SIGKILL: $REMAINING)..."
            pkill -9 -f "$target" || true
        fi
        echo "[✓] $target 进程已完全终止"
    else
        echo "[i] 未发现运行中的 $target"
    fi
done

# 3. 释放 20000 端口 (Web 服务端口)
PORT=20000
echo "[-] 正在检查端口 $PORT 占用情况..."

if command -v fuser >/dev/null 2>&1; then
    sudo fuser -k ${PORT}/tcp 2>/dev/null || true
fi

if command -v lsof >/dev/null 2>&1; then
    PORT_PIDS=$(lsof -ti:${PORT} || true)
    if [ -n "$PORT_PIDS" ]; then
        echo "[-] 强制终止占用端口 $PORT 的残留进程 (PID: $PORT_PIDS)..."
        kill -9 $PORT_PIDS 2>/dev/null || true
    fi
fi

# 4. 验证清理结果
sleep 1
CHECK_PIDS=$(pgrep -f "antminer_web.py|antminer_monitor.py|antminer_gui.py" || true)

echo "=================================================================="
if [ -z "$CHECK_PIDS" ]; then
    echo " [✓] 清理完成！所有 AntMiner 旧进程已全部终止，端口 $PORT 已成功释放。"
    echo " 现在可以安全启动新版本了。"
else
    echo " [!] 警告: 仍检测到以下残留进程 PID: $CHECK_PIDS"
    echo " 请尝试使用: sudo ./stop.sh 再次执行。"
fi
echo "=================================================================="
