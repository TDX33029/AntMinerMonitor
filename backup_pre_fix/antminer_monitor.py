#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AntMinerTab - Headless CLI & Service Monitor (Ubuntu 22.04 / Linux & Windows)
Target:
- Antminer S19 Hydro: http://10.8.1.86/#dashboard
- Mijia Smart Plug 3 (cuco.plug.v3): 10.8.1.110 (miIO UDP)
- Telegram Bot Alert: @s332854BOT (7775553661)

Features:
- Streamlined data acquisition: Queries /cgi-bin/stats.cgi and /cgi-bin/pools.cgi.
- HTTP Keep-Alive persistent connection to minimize miner load.
- Native miIO UDP driver for Mijia Smart Plug 3 (Power, Switch State, Temp).
- Dual Cutoff Protection:
  * WRN TEMP (default 75°C): Cut off power if maintained for >30s + Telegram alert.
  * STOP TEMP (default 78°C): Immediate unconditional power cutoff + Telegram alert.
  * Irreversible safety lock: No auto-recovery without explicit reset.
- Minimalist terminal dashboard (ANSI / TUI mode) with sparkline trends.
"""

import argparse
import hashlib
import json
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.adapters import HTTPAdapter
from requests.auth import HTTPDigestAuth


# Telegram Credentials
TELEGRAM_TOKEN = "8938811502:AAHSMmrELHYz8OrFmlD8YeaogjmZ7X7-7NE"
TELEGRAM_CHAT_ID = 7775553661


def send_telegram_async(message: str):
    """Send asynchronous HTML message to Telegram."""
    def _worker():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            }
            requests.post(url, json=payload, timeout=8)
        except Exception as e:
            sys.stderr.write(f"[Telegram Alert Error] {e}\n")

    threading.Thread(target=_worker, daemon=True).start()


# ANSI Colors & Controls
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GRAY = "\033[90m"
    CLEAR = "\033[2J\033[H"
    CURSOR_HOME = "\033[H"


def temp_color(t: float) -> str:
    """Return ANSI color based on temperature in Celsius."""
    if t >= 78.0:
        return Colors.RED
    elif t >= 68.0:
        return Colors.YELLOW
    elif t >= 55.0:
        return Colors.CYAN
    else:
        return Colors.GREEN


def render_sparkline(values: List[float], min_val: Optional[float] = None, max_val: Optional[float] = None) -> str:
    """Render 1D trend sparkline using Unicode height blocks ( ▂▃▄▅▆▇█)."""
    if not values:
        return ""
    blocks = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    actual_min = min(values)
    actual_max = max(values)
    val_min = min_val if min_val is not None else actual_min
    val_max = max_val if max_val is not None else actual_max
    span = val_max - val_min

    chars = []
    for v in values:
        c = temp_color(v)
        if span <= 0.001:
            idx = 3
        else:
            ratio = (v - val_min) / span
            idx = int(round(ratio * (len(blocks) - 1)))
            idx = max(0, min(len(blocks) - 1, idx))
        chars.append(f"{c}{blocks[idx]}{Colors.RESET}")
    return "".join(chars)


def render_horizontal_bar(val: float, max_range: float = 85.0, width: int = 12) -> str:
    """Render square-like horizontal level bar: [████████░░░░] 68.0°C"""
    ratio = max(0.0, min(1.0, val / max_range))
    fill_len = int(round(ratio * width))
    empty_len = width - fill_len
    c = temp_color(val)
    bar = f"{c}{'█' * fill_len}{Colors.GRAY}{'░' * empty_len}{Colors.RESET}"
    return f"[{bar}] {c}{val:4.1f}°C{Colors.RESET}"


class MijiaPlugDriver:
    """Robust native miIO UDP client with persistent socket & timestamp sync."""

    def __init__(self, ip: str = "10.8.1.110", token_hex: str = "458a4ef63ff154e2136a1342395c630f", did: str = "2051114902"):
        self.ip = ip
        self.did = str(did)
        self.token = bytes.fromhex(token_hex)
        self.key = hashlib.md5(self.token).digest()
        self.iv = hashlib.md5(self.key + self.token).digest()

        self.device_id: Optional[bytes] = None
        self.stamp_offset: Optional[int] = None
        self._msg_id = 0

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.5)

    def _encrypt(self, plaintext: bytes) -> bytes:
        pad_len = 16 - (len(plaintext) % 16)
        padded = plaintext + bytes([pad_len] * pad_len)
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def _decrypt(self, ciphertext: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        return padded[:-padded[-1]]

    def _handshake(self):
        hello = bytes.fromhex("21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
        self.sock.sendto(hello, (self.ip, 54321))
        data, _ = self.sock.recvfrom(1024)
        self.device_id = data[8:12]
        dev_stamp = int.from_bytes(data[12:16], "big")
        self.stamp_offset = dev_stamp - int(time.time())

    def _send_cmd(self, method: str, params: Any) -> Dict[str, Any]:
        for attempt in range(2):
            try:
                if self.device_id is None or self.stamp_offset is None:
                    self._handshake()

                self._msg_id += 1
                now_stamp = (int(time.time()) + self.stamp_offset) & 0xFFFFFFFF
                payload = json.dumps({"id": self._msg_id, "method": method, "params": params}, separators=(",", ":")).encode("utf-8")
                encrypted = self._encrypt(payload)

                packet_len = 32 + len(encrypted)
                header = bytearray(32)
                header[0:2] = b"\x21\x31"
                header[2:4] = packet_len.to_bytes(2, "big")
                header[8:12] = self.device_id
                header[12:16] = now_stamp.to_bytes(4, "big")
                header[16:32] = self.token
                header[16:32] = hashlib.md5(header[:16] + self.token + encrypted).digest()

                self.sock.sendto(header + encrypted, (self.ip, 54321))
                res, _ = self.sock.recvfrom(4096)
                decrypted = self._decrypt(res[32:])
                return json.loads(decrypted.decode("utf-8"))
            except Exception:
                self.device_id = None
                self.stamp_offset = None
                if attempt == 1:
                    raise
        raise TimeoutError("miIO communication failed after retry")

    def query_status(self) -> Dict[str, Any]:
        try:
            props = [
                {"did": self.did, "siid": 2, "piid": 1},   # Switch Status (bool)
                {"did": self.did, "siid": 11, "piid": 2},  # Electric Power (W)
                {"did": self.did, "siid": 12, "piid": 2},  # Internal Temp (°C)
            ]
            res_json = self._send_cmd("get_properties", props)

            switch_on = False
            power_w = 0.0
            temp_c = 0

            for item in res_json.get("result", []):
                siid = item.get("siid")
                piid = item.get("piid")
                val = item.get("value")
                if siid == 2 and piid == 1:
                    switch_on = bool(val)
                elif siid == 11 and piid == 2:
                    power_w = float(val) if val is not None else 0.0
                elif siid == 12 and piid == 2:
                    temp_c = int(val) if val is not None else 0

            return {
                "online": True,
                "switch_on": switch_on,
                "power_w": power_w,
                "plug_temp": temp_c,
            }
        except Exception as e:
            return {
                "online": False,
                "error": str(e),
                "switch_on": False,
                "power_w": 0.0,
                "plug_temp": 0,
            }

    def set_switch(self, state: bool) -> bool:
        try:
            props = [{"did": self.did, "siid": 2, "piid": 1, "value": bool(state)}]
            res = self._send_cmd("set_properties", props)
            for item in res.get("result", []):
                if item.get("code") == 0:
                    return True
            return False
        except Exception:
            return False


class AntminerMonitor:
    def __init__(
        self,
        base_url: str = "http://10.8.1.86",
        username: str = "root",
        password: str = "dl.general",
        plug_ip: str = "10.8.1.110",
        plug_token: str = "458a4ef63ff154e2136a1342395c630f",
        wrn_temp: float = 75.0,
        stop_temp: float = 78.0,
        interval: float = 1.0,
        timeout: float = 2.5,
        history_len: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.wrn_temp = wrn_temp
        self.stop_temp = stop_temp
        self.interval = interval
        self.timeout = timeout
        self.history_len = history_len

        # Persistent HTTP Session with Keep-Alive
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(self.username, self.password)
        adapter = HTTPAdapter(
            pool_connections=1,
            pool_maxsize=2,
            max_retries=1,
            pool_block=False
        )
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "Connection": "keep-alive",
            "User-Agent": "AntMinerTab-CLI/1.0",
        })

        # Plug Driver
        self.plug = MijiaPlugDriver(ip=plug_ip, token_hex=plug_token, did="2051114902")

        # Overheat state
        self.overheat_wrn_seconds = 0
        self.cutoff_triggered = False

        # History queues for sparklines
        self.history_hashrate: Deque[float] = deque(maxlen=self.history_len)
        self.history_inlet: Deque[float] = deque(maxlen=self.history_len)
        self.history_outlet: Deque[float] = deque(maxlen=self.history_len)
        self.history_chip_max: Deque[float] = deque(maxlen=self.history_len)

    def fetch_metrics(self) -> Dict[str, Any]:
        """Fetch miner & plug metrics."""
        # 1. Fetch Stats
        resp_stats = self.session.get(f"{self.base_url}/cgi-bin/stats.cgi", timeout=self.timeout)
        if resp_stats.status_code == 401:
            raise PermissionError("401 Unauthorized: Digest Authentication failed.")
        raw_stats = resp_stats.json()

        # 2. Fetch Pools
        resp_pools = self.session.get(f"{self.base_url}/cgi-bin/pools.cgi", timeout=self.timeout)
        raw_pools = resp_pools.json()

        # 3. Fetch Plug
        plug_data = self.plug.query_status()

        # Parse Stats
        st0 = raw_stats.get("STATS", [{}])[0]
        rate_5s_ghs = float(st0.get("rate_5s", 0.0))
        rate_5s_ths = rate_5s_ghs / 1000.0

        chain0 = st0.get("chain", [{}])[0]
        temp_pic = [float(x) for x in chain0.get("temp_pic", [])]
        temp_pcb = [float(x) for x in chain0.get("temp_pcb", [])]
        temp_chip = [float(x) for x in chain0.get("temp_chip", [])]

        inlet_t = temp_pic[0] if len(temp_pic) > 0 else 0.0
        outlet_t = temp_pic[1] if len(temp_pic) > 1 else 0.0
        delta_t = (outlet_t - inlet_t) if (inlet_t > 0 and outlet_t > 0) else 0.0

        chip_max = max(temp_chip) if temp_chip else 0.0
        chip_avg = (sum(temp_chip) / len(temp_chip)) if temp_chip else 0.0

        # Parse Pools
        total_accepted = 0
        total_rejected = 0
        total_diffa = 0.0
        total_diffr = 0.0
        for p in raw_pools.get("POOLS", []):
            total_accepted += int(p.get("accepted", 0))
            total_rejected += int(p.get("rejected", 0))
            total_diffa += float(p.get("diffa", 0.0))
            total_diffr += float(p.get("diffr", 0.0))

        total_diff = total_diffa + total_diffr
        reject_ratio = (total_diffr / total_diff * 100.0) if total_diff > 0 else 0.0

        # Push to history
        self.history_hashrate.append(rate_5s_ths)
        if inlet_t > 0:
            self.history_inlet.append(inlet_t)
        if outlet_t > 0:
            self.history_outlet.append(outlet_t)
        if chip_max > 0:
            self.history_chip_max.append(chip_max)

        # Protection evaluation
        self._evaluate_protection(chip_max, chip_avg, inlet_t, outlet_t, delta_t, plug_data)

        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "hashrate_ghs": rate_5s_ghs,
            "accepted_shares": total_accepted,
            "rejected_shares": total_rejected,
            "reject_ratio": reject_ratio,
            "water_inlet": inlet_t,
            "water_outlet": outlet_t,
            "water_delta": delta_t,
            "chip_max": chip_max,
            "chip_avg": chip_avg,
            "pcb_temps": temp_pcb,
            "chip_temps": temp_chip,
            "plug": plug_data,
            "cutoff_triggered": self.cutoff_triggered,
            "overheat_wrn_seconds": self.overheat_wrn_seconds,
            "history": {
                "hashrate": list(self.history_hashrate),
                "inlet": list(self.history_inlet),
                "outlet": list(self.history_outlet),
                "chip_max": list(self.history_chip_max),
            }
        }

    def _evaluate_protection(self, chip_max: float, chip_avg: float, tin: float, tout: float, dt: float, plug_data: dict):
        """Dual threshold overheat cutoff logic."""
        if self.cutoff_triggered:
            # Enforce off
            if plug_data.get("switch_on", False):
                self.plug.set_switch(False)
            return

        if chip_max >= self.stop_temp:
            # Immediate cutoff
            self.cutoff_triggered = True
            self.plug.set_switch(False)
            msg = (
                f"🚨 <b>[AntMinerTab-CLI 紧急急停断电告警]</b>\n\n"
                f"<b>设备</b>: Antminer S19 Hydro ({self.base_url})\n"
                f"<b>级别</b>: <b>CRITICAL (达到急停温度 STOP TEMP)</b>\n"
                f"<b>芯片温度</b>: <b>{chip_max:.1f}°C</b> (急停阈值: {self.stop_temp:.0f}°C)\n"
                f"<b>水冷回路</b>: 进水 {tin:.1f}°C | 出水 {tout:.1f}°C (ΔT: +{dt:.1f}°C)\n"
                f"<b>动作执行</b>: 米家智能插座(10.8.1.110)已立即<b>硬件切断电源</b>！\n"
                f"<b>保护机制</b>: 保护锁已激活，<b>绝不自动恢复</b>，请现场检查水冷！\n"
                f"<b>时间</b>: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            send_telegram_async(msg)

        elif chip_max >= self.wrn_temp:
            # WRN temp countdown (30 seconds)
            self.overheat_wrn_seconds += 1
            if self.overheat_wrn_seconds >= 30:
                self.cutoff_triggered = True
                self.plug.set_switch(False)
                msg = (
                    f"⚠️ <b>[AntMinerTab-CLI 超温持续超时断电告警]</b>\n\n"
                    f"<b>设备</b>: Antminer S19 Hydro ({self.base_url})\n"
                    f"<b>级别</b>: <b>WARNING 超时关机 (WRN TEMP 维持超30秒)</b>\n"
                    f"<b>芯片最高温</b>: <b>{chip_max:.1f}°C</b> (预警阈值: {self.wrn_temp:.0f}°C)\n"
                    f"<b>芯片平均温</b>: {chip_avg:.1f}°C\n"
                    f"<b>水冷回路</b>: 进水 {tin:.1f}°C | 出水 {tout:.1f}°C (ΔT: +{dt:.1f}°C)\n"
                    f"<b>动作执行</b>: 米家智能插座(10.8.1.110)已执行<b>断电保护</b>！\n"
                    f"<b>保护机制</b>: 保护锁已激活，<b>绝不自动恢复</b>！\n"
                    f"<b>时间</b>: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                send_telegram_async(msg)
        else:
            self.overheat_wrn_seconds = 0

    def print_dashboard(self, data: Dict[str, Any], watch_mode: bool = False):
        """Render a clean, minimalist terminal dashboard."""
        out = []
        if watch_mode:
            out.append(Colors.CURSOR_HOME)

        out.append("=" * 76)
        out.append(f"{Colors.BOLD}{Colors.CYAN} AntMinerTab (Ubuntu 22.04 LTS & Linux Edition){Colors.RESET}   {data['timestamp']}")
        out.append("=" * 76)

        # 1. KPI Line: Hashrate, Accepted, Rejected, Plug Power & State
        h_spark = render_sparkline(data["history"]["hashrate"])
        h_str = f"{data['hashrate_ghs']:,.1f} GH/s"
        plug = data.get("plug", {})
        pw_str = f"{plug.get('power_w', 0.0):.0f} W" if plug.get("online") else "OFFLINE"
        sw_str = f"{Colors.GREEN}ON{Colors.RESET}" if plug.get("switch_on") else f"{Colors.RED}OFF{Colors.RESET}"

        out.append(f" {Colors.BOLD}Real-time Hashrate:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{h_str}{Colors.RESET} [{h_spark}]   {Colors.BOLD}Accepted:{Colors.RESET} {Colors.GREEN}{data['accepted_shares']:,}{Colors.RESET}")
        out.append(f" {Colors.BOLD}Smart Plug (10.8.1.110):{Colors.RESET} Power: {Colors.YELLOW}{pw_str}{Colors.RESET} | Switch: [{sw_str}] | Temp: {plug.get('plug_temp', 0)}°C")
        out.append(f" {Colors.BOLD}Rejected Shares:{Colors.RESET}    {Colors.RED}{data['rejected_shares']}{Colors.RESET} (Ratio: {data['reject_ratio']:.2f}%)")
        out.append("-" * 76)

        # 2. Water Temperatures
        tin = data["water_inlet"]
        tout = data["water_outlet"]
        dt = data["water_delta"]
        in_spark = render_sparkline(data["history"]["inlet"])
        out_spark = render_sparkline(data["history"]["outlet"])

        out.append(f" {Colors.BOLD}WATER TEMPERATURE:{Colors.RESET}")
        out.append(f"   Inlet:   {temp_color(tin)}{tin:4.1f}°C{Colors.RESET}  [{in_spark:<10}]")
        out.append(f"   Outlet:  {temp_color(tout)}{tout:4.1f}°C{Colors.RESET}  [{out_spark:<10}]   (Delta T: {Colors.CYAN}+{dt:3.1f}°C{Colors.RESET})")
        out.append("-" * 76)

        # 3. Board Sensors (PCB & Chip Die)
        pcb_list = data["pcb_temps"]
        chip_list = data["chip_temps"]

        out.append(f" {Colors.BOLD}BOARD TEMPERATURE SENSORS (BOARD #1):{Colors.RESET}  [WRN: {self.wrn_temp:.0f}°C | STOP: {self.stop_temp:.0f}°C]")

        # Warning / Cutoff Banner
        if data.get("cutoff_triggered"):
            out.append(f"   {Colors.RED}{Colors.BOLD}🔴 EMERGENCY CUTOFF TRIGGERED! Plug cut off. Lock active.{Colors.RESET}")
        elif data.get("overheat_wrn_seconds", 0) > 0:
            rem = max(0, 30 - data["overheat_wrn_seconds"])
            out.append(f"   {Colors.YELLOW}{Colors.BOLD}⚠️ WRN TEMP EXCEEDED ({data['chip_max']:.1f}°C)! Cutoff in {rem}s...{Colors.RESET}")

        out.append(f"   {Colors.BOLD}PCB Surface Sensors:{Colors.RESET}")
        half = (len(pcb_list) + 1) // 2
        for i in range(half):
            col1 = f"PCB-{i+1} {render_horizontal_bar(pcb_list[i], 80.0, 11)}" if i < len(pcb_list) else ""
            j = i + half
            col2 = f"PCB-{j+1} {render_horizontal_bar(pcb_list[j], 80.0, 11)}" if j < len(pcb_list) else ""
            out.append(f"     {col1:<32}   {col2}")

        out.append(f"\n   {Colors.BOLD}Chip Die Core Sensors:{Colors.RESET}")
        half_c = (len(chip_list) + 1) // 2
        for i in range(half_c):
            col1 = f"Chip-{i+1} {render_horizontal_bar(chip_list[i], 85.0, 11)}" if i < len(chip_list) else ""
            j = i + half_c
            col2 = f"Chip-{j+1} {render_horizontal_bar(chip_list[j], 85.0, 11)}" if j < len(chip_list) else ""
            out.append(f"     {col1:<32}   {col2}")

        out.append("=" * 76 + ("\n" if not watch_mode else ""))
        sys.stdout.write("\n".join(out))
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(
        description="AntMinerTab Headless CLI & Daemon (Ubuntu 22.04 LTS)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--url", default="http://10.8.1.86", help="Miner Web URL")
    parser.add_argument("--user", default="root", help="Digest Auth User")
    parser.add_argument("--pwd", default="dl.general", help="Digest Auth Password")
    parser.add_argument("--plug-ip", default="10.8.1.110", help="Mijia Plug IP")
    parser.add_argument("--plug-token", default="458a4ef63ff154e2136a1342395c630f", help="Mijia Plug Token")
    parser.add_argument("--wrn-temp", type=float, default=75.0, help="Warning Temp threshold for 30s cutoff (°C)")
    parser.add_argument("--stop-temp", type=float, default=78.0, help="Emergency Stop Temp threshold for instant cutoff (°C)")
    parser.add_argument("--watch", "-w", action="store_true", help="Continuous monitoring TUI mode")
    parser.add_argument("--interval", "-i", type=float, default=1.0, help="Polling interval in seconds")
    parser.add_argument("--json", action="store_true", help="Output raw JSON format")

    args = parser.parse_args()

    monitor = AntminerMonitor(
        base_url=args.url,
        username=args.user,
        password=args.pwd,
        plug_ip=args.plug_ip,
        plug_token=args.plug_token,
        wrn_temp=args.wrn_temp,
        stop_temp=args.stop_temp,
        interval=args.interval,
    )

    if args.watch and not args.json:
        sys.stdout.write(Colors.CLEAR)
        sys.stdout.flush()

    try:
        while True:
            try:
                data = monitor.fetch_metrics()
                if args.json:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                else:
                    monitor.print_dashboard(data, watch_mode=args.watch)
            except Exception as e:
                sys.stderr.write(f"\n{Colors.RED}[Error] Monitoring failure: {e}{Colors.RESET}\n")
                if not args.watch:
                    sys.exit(1)

            if not args.watch:
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nAntMinerTab stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
