# AntMinerTab - Ubuntu 22.04 LTS 部署与运行指南

本套件已完全转写并适配 **Ubuntu 22.04 LTS**（兼容 Ubuntu 20.04 / 24.04 及 Debian 系列系统），包含两种运行形态：
1. **桌面图形模式 (PyQt5 GUI)**：`antminer_gui.py` —— 适合 Ubuntu Desktop，纯方形极简工业暗色界面，具备 1 小时波形、图上十字花直接测温、密码通电与双阈值断电保护。
2. **服务端无头守护模式 (Headless Daemon / CLI)**：`antminer_monitor.py` —— 适合 Ubuntu Server 或 SSH 远程会话，自带米家插座守护、双阈值断电熔断与 Telegram 报警。

---

## 1. 快速一键安装 (推荐)

在 Ubuntu 终端中进入项目目录，执行一键配置脚本：

```bash
chmod +x setup_ubuntu.sh
./setup_ubuntu.sh
```

脚本会自动通过 Ubuntu 官方 APT 源安装原生编译的 `PyQt5`、`pyqtgraph`、`cryptography` 与 `requests`，**完美避开 pip 源码编译依赖缺失的问题**，只需约 30 秒即可装毕。

---

## 2. 手动安装系统依赖

如果您希望手动安装，只需在 Ubuntu 终端中输入：

```bash
# 1. 更新软件源
sudo apt update -y

# 2. 安装 Python3 与所需库（原生 APT 二进制包，零编译错误）
sudo apt install -y \
    python3 \
    python3-pip \
    python3-requests \
    python3-cryptography \
    python3-pyqt5 \
    python3-pyqtgraph \
    fonts-dejavu \
    fonts-ubuntu

# 3. 赋予执行权限
chmod +x antminer_gui.py antminer_monitor.py
```

---

## 3. 运行方式

### 方式 A：启动 PyQt5 桌面图形监控大屏 (Ubuntu Desktop)
如果您在 Ubuntu 图形界面（GNOME / X11 / Wayland）下运行：

```bash
python3 antminer_gui.py
```

* **特性**：
  * 标题：`AntMinerTab`
  * 自动适配 Ubuntu 原生等宽字体栈（`Ubuntu Mono` / `DejaVu Sans Mono`）。
  * 顶部配备**自定义刷新时间数值微调框**（`QDoubleSpinBox`，支持 0.5s~60.0s 任意自由调节/键盘手动输入，即时生效）。
  * 默认 1 秒高频极速刷新、**1 小时真实本地时钟时间轴**（底轴清晰显示 `HH:MM`，局部放大自适应展示 `HH:MM:SS`）。
  * 鼠标在折线图内移动时显示**零抖动穿透竖线**，并在 **4 条折线上直接显示十字花标记（`+`）与右上角悬浮温度数值框，实时标注采样点的绝对发生时刻**。
  * 具备 `WRN TEMP`（默认 75°C 维持 30s 熔断）与 `STOP TEMP`（默认 78°C 即刻熔断）保护，超温断电时自动向 Telegram 发送报警。
  * 启动插座电源需要输入授权密码：`dl.general`。

---

### 方式 B：启动终端控制台守护进程 (Ubuntu Server / SSH)
如果您在没有桌面环境的纯命令行服务器中运行，或在 tmux / screen 中挂载：

```bash
# 默认 1 秒轮询，带 TUI 原地平滑刷新大屏与后台温度熔断守护
python3 antminer_monitor.py --watch

# 自定义刷新时间数值运行（例如 2.5 秒刷新一次）
python3 antminer_monitor.py --watch --interval 2.5

# 自定义阈值运行
python3 antminer_monitor.py --watch --wrn-temp 74.0 --stop-temp 77.0
```

---

### 方式 C：启动 Web 网页大屏服务 (端口 20000，支持 Tailscale 远程访问)
适合希望在局域网内任意手机、电脑，或通过 **Tailscale 远程网络** 随时随地使用浏览器查看：

```bash
# 默认启动（开箱即用，默认开启密码保护，自动读取同目录下的 config.json）
python3 antminer_web.py
```

* **配置文件 `config.json`**：
  服务端在当前目录下维护 `config.json`，集中管理两层密码：
  ```json
  {
    "login_password": "antminer",
    "power_password": "dl.general"
  }
  ```
* **访问地址**：
  * **本机访问**：[http://127.0.0.1:20000](http://127.0.0.1:20000)
  * **Tailscale 远程访问**：`http://<您的Tailscale-IP>:20000`（服务已绑定 `0.0.0.0:20000`，Tailscale 虚拟网络内的所有设备可直接秒开）
  * **局域网设备访问**：`http://<Ubuntu局域网IP>:20000`
* **安全鉴权**：
  * **Web 登录认证**：默认强制开启。浏览器访问时弹出认证弹窗，**无需填写用户名（可留空或随意输入）**，只需输入 `config.json` 中配置的 `login_password`（默认 `antminer`）即可进入。
  * **通电操作保护**：网页端点击插座启动通电（ON）时，弹出密码输入框，需要输入 `config.json` 中配置的 `power_password`（默认 `dl.general`），校验通过后方可合闸送电。急停断电（OFF）无需密码。

---

### 方式 D：注册为 Ubuntu 系统级守护进程 (systemd 开机自启)
适合将这台 Ubuntu 主机作为矿场的 7x24 小时无人值守监控中枢，机器重启也能自动接管温度防护：

```bash
# 1. 假设项目放置在 /opt/AntMiner（如果放置在其他目录，请修改 antminer.service 中的路径）
sudo mkdir -p /opt/AntMiner
sudo cp -r ./* /opt/AntMiner/

# 2. 将服务文件安装到系统目录
sudo cp /opt/AntMiner/antminer.service /etc/systemd/system/

# 3. 重载 systemd 并启动服务
sudo systemctl daemon-reload
sudo systemctl enable antminer.service
sudo systemctl start antminer.service

# 4. 查看运行状态与日志
sudo systemctl status antminer.service
sudo journalctl -u antminer.service -f
```

---

## 4. 网络与防火墙配置提示

- **矿机 Web 地址**：`http://10.8.1.86`（HTTP 端口 80，使用 HTTP Keep-Alive 长连接）。
- **米家智能插座 3 地址**：`10.8.1.110`（使用 **UDP 54321 端口** 通信）。
  * 如果 Ubuntu 主机启用了 UFW 防火墙，请确保放行局域网通信：
    ```bash
    sudo ufw allow from 10.8.1.0/24
    ```
- **Telegram 报警通道**：
  * 使用 Telegram Bot API (`https://api.telegram.org`) 发送通知，请确保该 Ubuntu 主机具备访问互联网能力。

---

## 5. 项目文件清单

| 文件名 | 用途说明 |
| :--- | :--- |
| `antminer_web.py` | **Web 网页监控大屏服务**（监听 `0.0.0.0:20000`，支持本地与 Tailscale 远程访问） |
| `antminer_gui.py` | **PyQt5 桌面专业监控程序**（全功能图形界面、纯方形工业风、折线游标测温） |
| `antminer_monitor.py` | **轻量终端 CLI / 后台守护进程**（适合 Server / SSH / 无头模式） |
| `setup_ubuntu.sh` | **Ubuntu 22.04 LTS 一键环境依赖安装脚本** |
| `antminer.service` | **systemd 服务模板**（用于无人值守开机自启动） |
| `README_UBUNTU.md` | Ubuntu 专用部署运行文档 |
| `README.md` | 项目通用说明文档 |
