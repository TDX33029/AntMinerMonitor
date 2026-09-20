#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AntMinerTab - Web Server Edition (Listening on 0.0.0.0:20000)
Targets:
- Antminer S19 Hydro: http://10.8.1.86/#dashboard
- Mijia Smart Plug 3 (cuco.plug.v3): 10.8.1.110 (miIO UDP)
- Telegram Alarm Bot: @s332854BOT -> Chat ID 7775553661

Features:
- Binds to 0.0.0.0:20000, accessible via localhost (127.0.0.1:20000), LAN, and Tailscale (tailscale_ip:20000).
- Pure square industrial design (0px border-radius, dark theme matching AntMinerTab desktop GUI).
- Web-based 1-Hour dynamic temperature curve rendered with high-DPI smooth HTML5 Canvas:
  * 100% Responsive: Automatically scales and fills the webpage viewport (no fixed height).
  * Interactive Zoom & Pan: Mouse wheel zooms in/out (from 1min to 1hour), drag to pan, double-click to reset.
  * Crosshairs ('+') directly on all 4 curves with Top-Right temperature badges.
  * Auto right-align after 6s of no interaction.
- Top 5 Metric Cards (Hashrate, Accepted, Rejected & Ratio, Plug Power, Interactive Plug Switch with password dl.general).
- Dual Cutoff Protection (WRN TEMP 30s cutoff, STOP TEMP instant cutoff, no auto-recovery, Telegram alert).
- Zero external third-party web framework dependencies (built on Python standard library ThreadingHTTPServer).
"""

import argparse
import base64
import bisect
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.adapters import HTTPAdapter
from requests.auth import HTTPDigestAuth


# Telegram Credentials
TELEGRAM_TOKEN = "8938811502:AAHSMmrELHYz8OrFmlD8YeaogjmZ7X7-7NE"
TELEGRAM_CHAT_ID = 7775553661


def send_telegram_async(message: str):
    """Send asynchronous HTML message to Telegram without blocking."""
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
        self._lock = threading.Lock()

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
        with self._lock:
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


class MonitorStateManager:
    """Central state & background poller with persistent HTTP and UDP sessions."""

    def __init__(
        self,
        miner_url: str = "http://10.8.1.86",
        username: str = "root",
        password: str = "dl.general",
        plug_ip: str = "10.8.1.110",
        plug_token: str = "458a4ef63ff154e2136a1342395c630f",
        interval: float = 1.0,
        wrn_temp: float = 75.0,
        stop_temp: float = 78.0,
    ):
        self.miner_url = miner_url.rstrip("/")
        self.username = username
        self.password = password
        self.interval = interval
        self.wrn_temp = wrn_temp
        self.stop_temp = stop_temp
        self.paused = False

        self.lock = threading.Lock()

        # Miner Session with Keep-Alive & Self-Healing
        self.session = self._create_miner_session()

        # Last Valid Miner Metrics (for debouncing & hold-last-good during transient network drops)
        self.last_valid_metrics: Optional[Dict[str, Any]] = None
        self.consecutive_miner_failures: int = 0
        self.max_hold_failures: int = 5  # Hold values for up to 5 consecutive transient failed attempts

        # Plug Driver
        self.plug = MijiaPlugDriver(ip=plug_ip, token_hex=plug_token, did="2051114902")

        # 1-Hour buffers (3600 seconds)
        self.window_seconds = 3600.0
        self.time_buffer: Deque[float] = deque()
        self.inlet_buffer: Deque[float] = deque()
        self.outlet_buffer: Deque[float] = deque()
        self.chip_max_buffer: Deque[float] = deque()
        self.chip_avg_buffer: Deque[float] = deque()

        # Protection State
        self.overheat_wrn_seconds = 0
        self.cutoff_triggered = False

        # Latest Snapshot
        self.latest_data: Dict[str, Any] = {
            "timestamp": "--:--:--",
            "latency_ms": 0,
            "miner_online": False,
            "miner_recovering": False,
            "hashrate_ghs": 0.0,
            "accepted_shares": 0,
            "rejected_shares": 0,
            "reject_ratio": 0.0,
            "water_inlet": 0.0,
            "water_outlet": 0.0,
            "water_delta": 0.0,
            "pcb_temps": [],
            "chip_temps": [],
            "chip_max": 0.0,
            "chip_avg": 0.0,
            "plug": {"online": False, "switch_on": False, "power_w": 0.0, "plug_temp": 0},
            "wrn_temp": self.wrn_temp,
            "stop_temp": self.stop_temp,
            "interval": self.interval,
            "cutoff_triggered": False,
            "overheat_wrn_seconds": 0,
        }

        # Start Background Thread
        self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.worker_thread.start()

    def _create_miner_session(self) -> requests.Session:
        s = requests.Session()
        s.auth = HTTPDigestAuth(self.username, self.password)
        adapter = HTTPAdapter(
            pool_connections=2,
            pool_maxsize=4,
            max_retries=2,
            pool_block=False
        )
        s.mount("http://", adapter)
        s.headers.update({
            "Connection": "keep-alive",
            "User-Agent": "AntMinerTab-Web/2.0",
        })
        return s

    def _reinit_miner_session(self):
        """Self-heal stale Keep-Alive sockets and reset HTTP Digest auth state."""
        try:
            if hasattr(self, "session") and self.session:
                self.session.close()
        except Exception:
            pass
        self.session = self._create_miner_session()

    def set_settings(self, interval: Optional[float] = None, wrn: Optional[float] = None, stop: Optional[float] = None, paused: Optional[bool] = None):
        with self.lock:
            if interval is not None:
                self.interval = max(0.5, float(interval))
                self.latest_data["interval"] = self.interval
            if wrn is not None:
                self.wrn_temp = float(wrn)
                self.latest_data["wrn_temp"] = self.wrn_temp
            if stop is not None:
                self.stop_temp = float(stop)
                self.latest_data["stop_temp"] = self.stop_temp
            if paused is not None:
                self.paused = bool(paused)
                self.latest_data["paused"] = self.paused

    def set_plug_switch(self, state: bool) -> bool:
        """Switch plug power state."""
        res = self.plug.set_switch(state)
        st = self.plug.query_status()
        with self.lock:
            self.latest_data["plug"] = st
        return res

    def reset_protection_lock(self):
        with self.lock:
            self.cutoff_triggered = False
            self.overheat_wrn_seconds = 0
            self.latest_data["cutoff_triggered"] = False
            self.latest_data["overheat_wrn_seconds"] = 0

        rst_msg = (
            "ℹ️ <b>[AntMinerTab-Web 保护锁复位通知]</b>\n\n"
            "用户已在 Web 监控页面手动复位了超温断电安全保护锁。\n"
            "系统已恢复正常监控，如需通电请点击开关并输入授权密码。\n"
            f"<b>时间</b>: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram_async(rst_msg)

    def clear_history(self):
        """Clear all historical 1-hour curve data points."""
        with self.lock:
            self.time_buffer.clear()
            self.inlet_buffer.clear()
            self.outlet_buffer.clear()
            self.chip_max_buffer.clear()
            self.chip_avg_buffer.clear()

    def _run_loop(self):
        last_check_time = time.time()
        while True:
            t0 = time.time()
            dt = max(0.1, min(60.0, t0 - last_check_time))
            last_check_time = t0
            with self.lock:
                cur_interval = self.interval
                is_paused = self.paused
                wrn_thresh = self.wrn_temp
                stop_thresh = self.stop_temp

            if not is_paused:
                miner_fetch_success = False
                miner_err = ""
                rate_5s_ghs = 0.0
                total_accepted = 0
                total_rejected = 0
                reject_ratio = 0.0
                temp_pic = []
                temp_pcb = []
                temp_chip = []

                # 1. Fetch Miner Data (with socket connect/read timeout & JSON validation)
                try:
                    resp_stats = self.session.get(f"{self.miner_url}/cgi-bin/stats.cgi", timeout=(1.5, 3.5))
                    if resp_stats.status_code != 200:
                        raise ValueError(f"stats.cgi returned HTTP {resp_stats.status_code}")
                    raw_stats = resp_stats.json()

                    resp_pools = self.session.get(f"{self.miner_url}/cgi-bin/pools.cgi", timeout=(1.5, 3.5))
                    if resp_pools.status_code != 200:
                        raise ValueError(f"pools.cgi returned HTTP {resp_pools.status_code}")
                    raw_pools = resp_pools.json()

                    st0 = raw_stats.get("STATS", [{}])[0]
                    rate_5s_ghs = float(st0.get("rate_5s", 0.0))

                    chain0 = st0.get("chain", [{}])[0]
                    temp_pic = [float(x) for x in chain0.get("temp_pic", [])]
                    temp_pcb = [float(x) for x in chain0.get("temp_pcb", [])]
                    temp_chip = [float(x) for x in chain0.get("temp_chip", [])]

                    total_diffa = 0.0
                    total_diffr = 0.0
                    for p in raw_pools.get("POOLS", []):
                        total_accepted += int(p.get("accepted", 0))
                        total_rejected += int(p.get("rejected", 0))
                        total_diffa += float(p.get("diffa", 0.0))
                        total_diffr += float(p.get("diffr", 0.0))

                    total_diff = total_diffa + total_diffr
                    reject_ratio = (total_diffr / total_diff * 100.0) if total_diff > 0 else 0.0

                    miner_fetch_success = True
                    self.consecutive_miner_failures = 0
                    self.last_valid_metrics = {
                        "rate_5s_ghs": rate_5s_ghs,
                        "total_accepted": total_accepted,
                        "total_rejected": total_rejected,
                        "reject_ratio": reject_ratio,
                        "temp_pic": temp_pic,
                        "temp_pcb": temp_pcb,
                        "temp_chip": temp_chip,
                    }
                except Exception as e:
                    miner_err = str(e)
                    self.consecutive_miner_failures += 1
                    # Self-heal stale Keep-Alive session periodically on repeated errors
                    if self.consecutive_miner_failures % 3 == 0:
                        self._reinit_miner_session()

                is_recovering = False
                if miner_fetch_success:
                    miner_ok = True
                elif self.last_valid_metrics and self.consecutive_miner_failures <= self.max_hold_failures:
                    # Debounce / hold last known good value to prevent zeroing metrics and chart gaps
                    rate_5s_ghs = self.last_valid_metrics["rate_5s_ghs"]
                    total_accepted = self.last_valid_metrics["total_accepted"]
                    total_rejected = self.last_valid_metrics["total_rejected"]
                    reject_ratio = self.last_valid_metrics["reject_ratio"]
                    temp_pic = self.last_valid_metrics["temp_pic"]
                    temp_pcb = self.last_valid_metrics["temp_pcb"]
                    temp_chip = self.last_valid_metrics["temp_chip"]
                    miner_ok = True
                    is_recovering = True
                else:
                    miner_ok = False

                # 2. Fetch Plug Data
                plug_data = self.plug.query_status()

                latency_ms = int((time.time() - t0) * 1000)

                inlet_t = temp_pic[0] if len(temp_pic) > 0 else 0.0
                outlet_t = temp_pic[1] if len(temp_pic) > 1 else 0.0
                delta_t = (outlet_t - inlet_t) if (inlet_t > 0 and outlet_t > 0) else 0.0

                chip_max = max(temp_chip) if temp_chip else 0.0
                chip_avg = (sum(temp_chip) / len(temp_chip)) if temp_chip else 0.0

                # 3. Dual Cutoff Protection Logic
                is_over_wrn = (chip_max >= wrn_thresh)
                is_over_stop = (chip_max >= stop_thresh)

                with self.lock:
                    if self.cutoff_triggered:
                        if plug_data.get("switch_on", False):
                            self.plug.set_switch(False)
                    else:
                        if is_over_stop:
                            self.cutoff_triggered = True
                            self.plug.set_switch(False)
                            msg = (
                                f"🚨 <b>[AntMinerTab-Web 紧急急停断电告警]</b>\n\n"
                                f"<b>设备</b>: Antminer S19 Hydro ({self.miner_url})\n"
                                f"<b>级别</b>: <b>CRITICAL (达到急停温度 STOP TEMP)</b>\n"
                                f"<b>芯片温度</b>: <b>{chip_max:.1f}°C</b> (急停阈值: {stop_thresh:.0f}°C)\n"
                                f"<b>水冷回路</b>: 进水 {inlet_t:.1f}°C | 出水 {outlet_t:.1f}°C (ΔT: +{delta_t:.1f}°C)\n"
                                f"<b>动作执行</b>: 米家智能插座已立即<b>硬件切断电源</b>！\n"
                                f"<b>保护机制</b>: 保护锁已死锁，<b>绝不自动恢复</b>，请现场检查水冷管路！\n"
                                f"<b>时间</b>: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                            send_telegram_async(msg)

                        elif is_over_wrn:
                            self.overheat_wrn_seconds = min(30.0, self.overheat_wrn_seconds + dt)
                            if self.overheat_wrn_seconds >= 30.0:
                                self.cutoff_triggered = True
                                self.plug.set_switch(False)
                                msg = (
                                    f"⚠️ <b>[AntMinerTab-Web 超温持续超时断电告警]</b>\n\n"
                                    f"<b>设备</b>: Antminer S19 Hydro ({self.miner_url})\n"
                                    f"<b>级别</b>: <b>WARNING 超时关机 (WRN TEMP 维持超30秒)</b>\n"
                                    f"<b>芯片最高温</b>: <b>{chip_max:.1f}°C</b> (预警阈值: {wrn_thresh:.0f}°C)\n"
                                    f"<b>芯片平均温</b>: {chip_avg:.1f}°C\n"
                                    f"<b>水冷回路</b>: 进水 {inlet_t:.1f}°C | 出水 {outlet_t:.1f}°C (ΔT: +{delta_t:.1f}°C)\n"
                                    f"<b>动作执行</b>: 米家智能插座已执行<b>断电保护</b>！\n"
                                    f"<b>保护机制</b>: 保护锁已激活，<b>绝不自动恢复</b>，请检查进出水温与散热！\n"
                                    f"<b>时间</b>: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                                )
                                send_telegram_async(msg)
                        elif miner_ok:
                            self.overheat_wrn_seconds = 0

                    # Append to 1-Hour buffers
                    now_epoch = time.time()
                    if inlet_t > 0 or outlet_t > 0 or chip_max > 0:
                        self.time_buffer.append(now_epoch)
                        self.inlet_buffer.append(inlet_t)
                        self.outlet_buffer.append(outlet_t)
                        self.chip_max_buffer.append(chip_max)
                        self.chip_avg_buffer.append(chip_avg)

                        cutoff_epoch = now_epoch - self.window_seconds
                        while self.time_buffer and self.time_buffer[0] < cutoff_epoch:
                            self.time_buffer.popleft()
                            self.inlet_buffer.popleft()
                            self.outlet_buffer.popleft()
                            self.chip_max_buffer.popleft()
                            self.chip_avg_buffer.popleft()

                    self.latest_data = {
                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                        "time_epoch": now_epoch,
                        "latency_ms": latency_ms,
                        "miner_online": miner_ok,
                        "miner_recovering": is_recovering,
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
                        "wrn_temp": self.wrn_temp,
                        "stop_temp": self.stop_temp,
                        "interval": self.interval,
                        "paused": self.paused,
                        "cutoff_triggered": self.cutoff_triggered,
                        "overheat_wrn_seconds": self.overheat_wrn_seconds,
                    }

            # Sleep slice
            sleep_step = 0.05
            slept = 0.0
            while slept < cur_interval:
                time.sleep(sleep_step)
                slept += sleep_step
                with self.lock:
                    cur_interval = self.interval

    def get_api_payload(self) -> Dict[str, Any]:
        with self.lock:
            data = dict(self.latest_data)
            now_epoch = time.time()
            data["now_epoch"] = now_epoch
            data["curves"] = {
                "x": list(self.time_buffer),
                "inlet": list(self.inlet_buffer),
                "outlet": list(self.outlet_buffer),
                "chip_max": list(self.chip_max_buffer),
                "chip_avg": list(self.chip_avg_buffer),
            }
            return data


# Embedded Fully-Responsive High-DPI Canvas HTML/CSS/JS Single-Page Dashboard
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AntMinerTab</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    height: 100%;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    background-color: #131417;
    color: #e0e2e8;
    font-family: "Consolas", "Ubuntu Mono", "DejaVu Sans Mono", monospace;
    padding: 10px 14px 8px 14px;
    user-select: none;
    overflow-x: hidden;
  }
  /* Pure Square Industrial Theme */
  .header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
    flex-wrap: wrap;
    flex-shrink: 0;
  }
  .title {
    font-size: 16px;
    font-weight: bold;
    color: #ffffff;
    letter-spacing: 0.5px;
  }
  .badge {
    font-size: 11px;
    font-weight: bold;
    padding: 2px 8px;
    border: 1px solid #4a3e20;
    background: #23211b;
    color: #f4a261;
  }
  .badge.connected {
    border-color: #1e4538;
    background: #14241e;
    color: #00c49f;
  }
  .badge.disconnected {
    border-color: #572635;
    background: #2b171c;
    color: #e76f51;
  }
  .controls {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: #868c9c;
  }
  input[type="number"] {
    background: #22252e;
    border: 1px solid #2d313b;
    color: #ffd166;
    font-family: inherit;
    font-weight: bold;
    font-size: 12px;
    padding: 2px 6px;
    width: 68px;
    border-radius: 0;
  }
  button {
    background: #22252e;
    border: 1px solid #2d313b;
    color: #e0e2e8;
    font-family: inherit;
    font-size: 12px;
    font-weight: bold;
    padding: 4px 12px;
    cursor: pointer;
    border-radius: 0;
  }
  button:hover {
    background: #2d313d;
    border-color: #00a8e8;
  }
  /* Top 5 Metric Cards */
  .cards-row {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 8px;
    margin-bottom: 8px;
    flex-shrink: 0;
  }
  @media (max-width: 900px) {
    .cards-row { grid-template-columns: repeat(2, 1fr); }
  }
  .card {
    background: #1b1d22;
    border: 1px solid #2d313b;
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    justifyContent: space-between;
  }
  .card-title {
    font-size: 10px;
    font-weight: bold;
    color: #868c9c;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }
  .card-value {
    font-size: 19px;
    font-weight: bold;
    margin: 2px 0;
  }
  .card-sub {
    font-size: 10px;
    color: #868c9c;
  }
  /* Plug Toggle Button */
  .btn-switch-on {
    background: #1a382c;
    border: 1px solid #00c49f;
    color: #00c49f;
    width: 100%;
    padding: 3px 0;
    margin: 2px 0;
  }
  .btn-switch-off {
    background: #3b1e24;
    border: 1px solid #e76f51;
    color: #e76f51;
    width: 100%;
    padding: 3px 0;
    margin: 2px 0;
  }
  /* Main Grid (Full Height Responsive) */
  .main-grid {
    flex: 1;
    display: grid;
    grid-template-columns: 6.3fr 3.7fr;
    gap: 10px;
    min-height: 380px;
    height: 100%;
  }
  @media (max-width: 900px) {
    .main-grid { grid-template-columns: 1fr; height: auto; }
  }
  .panel {
    background: #1b1d22;
    border: 1px solid #2d313b;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    height: 100%;
  }
  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
    font-size: 12px;
    font-weight: bold;
    flex-shrink: 0;
  }
  .water-banner {
    color: #00a8e8;
    font-size: 11px;
  }
  .zoom-hint {
    color: #868c9c;
    font-size: 10px;
    margin-left: 8px;
  }
  /* Canvas Plot Area (Flexible Height) */
  .plot-container {
    position: relative;
    width: 100%;
    flex: 1;
    min-height: 260px;
    background: #1b1d22;
    cursor: crosshair;
  }
  canvas#plotCanvas {
    position: absolute;
    top: 0; left: 0;
    width: 100%;
    height: 100%;
    display: block;
  }
  /* Sensors & Threshold Panel */
  .thresh-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    font-size: 11px;
    font-weight: bold;
    color: #868c9c;
    flex-shrink: 0;
  }
  .alert-banner {
    background: #3b171c;
    border: 1px solid #752834;
    color: #e76f51;
    font-size: 11px;
    font-weight: bold;
    padding: 4px 6px;
    margin-bottom: 6px;
    display: none;
    flex-shrink: 0;
  }
  .btn-reset-lock {
    background: #5e1f2b;
    border: 1px solid #e76f51;
    color: #ffffff;
    font-weight: bold;
    padding: 4px 8px;
    width: 100%;
    margin-bottom: 8px;
    display: none;
    flex-shrink: 0;
  }
  .sec-title {
    font-size: 11px;
    font-weight: bold;
    margin: 4px 0 2px 0;
    flex-shrink: 0;
  }
  .sensor-bar-row {
    display: flex;
    align-items: center;
    height: 22px;
    font-size: 11px;
    font-weight: bold;
  }
  .bar-label { width: 55px; color: #ffffff; }
  .bar-track {
    flex: 1;
    height: 8px;
    background: #252830;
    position: relative;
    margin: 0 8px;
  }
  .bar-fill {
    height: 100%;
    width: 0%;
    background: #00c49f;
  }
  .bar-val { width: 50px; text-align: right; }
  /* Footer */
  .footer {
    display: flex;
    justify-content: space-between;
    margin-top: 8px;
    font-size: 11px;
    color: #868c9c;
    flex-shrink: 0;
  }
  /* Password Modal */
  .modal-overlay {
    position: fixed;
    top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.7);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 999;
  }
  .modal-box {
    background: #1b1d22;
    border: 1px solid #2d313b;
    padding: 16px;
    width: 320px;
  }
  .modal-title {
    font-size: 13px;
    font-weight: bold;
    margin-bottom: 8px;
  }
  .modal-input {
    width: 100%;
    padding: 6px 8px;
    background: #22252e;
    border: 1px solid #2d313b;
    color: #ffffff;
    font-family: inherit;
    font-size: 13px;
    margin-bottom: 8px;
  }
  .modal-err {
    color: #e76f51;
    font-size: 11px;
    margin-bottom: 8px;
    display: none;
  }
  .modal-btns {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <span class="title">AntMinerTab</span>
  <span class="badge" id="statusBadge">[ CONNECTING... ]</span>
  <div class="controls">
    <span>Interval:</span>
    <input type="number" id="intervalInput" min="0.5" max="60" step="0.5" value="1.0">
    <button id="pauseBtn">Pause</button>
    <button id="clearChartBtn">Clear Chart</button>
  </div>
</div>

<!-- Top 5 Metric Cards -->
<div class="cards-row">
  <div class="card">
    <div class="card-title">Real-time Hashrate</div>
    <div class="card-value" id="valHashrate" style="color:#00a8e8">-- GH/s</div>
    <div class="card-sub">Real-time 5s hashrate</div>
  </div>
  <div class="card">
    <div class="card-title">Accepted Shares</div>
    <div class="card-value" id="valAccepted" style="color:#00c49f">--</div>
    <div class="card-sub">Cumulative accepted</div>
  </div>
  <div class="card">
    <div class="card-title">Rejected Shares</div>
    <div class="card-value" id="valRejected" style="color:#e76f51">--</div>
    <div class="card-sub" id="valRejectRatio">Ratio: -- %</div>
  </div>
  <div class="card">
    <div class="card-title">Smart Plug Power</div>
    <div class="card-value" id="valPlugPower" style="color:#f4a261">-- W</div>
    <div class="card-sub" id="valPlugLoad">Load: -- kW</div>
  </div>
  <div class="card">
    <div class="card-title">Plug Switch</div>
    <button id="plugSwitchBtn" class="btn-switch-on">ON</button>
    <div class="card-sub" id="valPlugTemp">Plug: --°C</div>
  </div>
</div>

<!-- Main Section (Flexible Height) -->
<div class="main-grid">
  <!-- Left Panel: Responsive 1-Hour Canvas Plot -->
  <div class="panel">
    <div class="panel-header">
      <div>
        <span style="color:#868c9c">TEMPERATURE DYNAMICS</span>
        <span class="zoom-hint" id="zoomHint">(Scroll to zoom, drag to pan, dblclick to reset)</span>
      </div>
      <span class="water-banner" id="waterBanner">Inlet: --.-°C | Outlet: --.-°C (ΔT: --.-°C)</span>
    </div>
    <div class="plot-container" id="plotWrapper">
      <canvas id="plotCanvas"></canvas>
    </div>
  </div>

  <!-- Right Panel: Dual Thresholds & Sensor Gauges -->
  <div class="panel">
    <div class="panel-header">
      <span style="color:#868c9c">BOARD SENSORS (#1)</span>
    </div>

    <!-- Threshold Inputs -->
    <div class="thresh-row">
      <span>WRN:</span>
      <input type="number" id="wrnSpin" min="40" max="90" step="1" value="75"> °C
      <span style="margin-left:6px">STOP:</span>
      <input type="number" id="stopSpin" min="45" max="95" step="1" value="78"> °C
    </div>

    <div class="alert-banner" id="alertBanner"></div>
    <button class="btn-reset-lock" id="resetLockBtn">RESET PROTECTION LOCK</button>

    <!-- PCB Sensors -->
    <div class="sec-title" style="color:#00a8e8">PCB Surface Sensors</div>
    <div id="pcbBarsContainer"></div>

    <!-- Chip Die Sensors -->
    <div class="sec-title" style="color:#f4a261; margin-top:6px">Chip Die Core Sensors</div>
    <div id="chipBarsContainer"></div>
  </div>
</div>

<!-- Footer -->
<div class="footer">
  <span>Miner: 10.8.1.86 | Plug: 10.8.1.110 (miIO) | Tailscale/LAN Port 20000</span>
  <span id="footerUpdated">Last update: --:--:--</span>
</div>

<!-- Password Modal -->
<div class="modal-overlay" id="pwdModal">
  <div class="modal-box">
    <div class="modal-title">Power ON Authentication</div>
    <div style="font-size:11px; color:#868c9c; margin-bottom:8px">Enter password to turn ON the smart plug:</div>
    <input type="password" class="modal-input" id="pwdInput" placeholder="Password">
    <div class="modal-err" id="pwdErr">Incorrect password! Try again.</div>
    <div class="modal-btns">
      <button id="pwdConfirmBtn">CONFIRM</button>
      <button id="pwdCancelBtn">CANCEL</button>
    </div>
  </div>
</div>

<script>
  let isPaused = false;
  let currentPlugState = true;
  let curvesData = { x: [], inlet: [], outlet: [], chip_max: [], chip_avg: [] };
  let currentServerNow = Date.now() / 1000;

  function formatClock(epochSec, withSec = false) {
    if (!epochSec || epochSec <= 0) return "--:--";
    const d = new Date(epochSec * 1000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    if (withSec) {
      const ss = String(d.getSeconds()).padStart(2, '0');
      return `${hh}:${mm}:${ss}`;
    }
    return `${hh}:${mm}`;
  }

  // Responsive Zoom & Pan Viewport state
  let viewSpan = 3600;    // Visible time span in seconds (Default 1 hour)
  let viewOffset = 0;     // Shift right edge away from current time (Default 0 = now)
  let hoverX = null;
  let lastMouseMoveTime = 0;
  let isDragging = false;
  let dragStartX = 0;
  let dragStartOffset = 0;

  // Initialize Bars
  const pcbContainer = document.getElementById("pcbBarsContainer");
  const chipContainer = document.getElementById("chipBarsContainer");

  for (let i = 1; i <= 6; i++) {
    pcbContainer.innerHTML += `
      <div class="sensor-bar-row">
        <span class="bar-label">PCB-${i}</span>
        <div class="bar-track"><div class="bar-fill" id="pcbFill${i}"></div></div>
        <span class="bar-val" id="pcbVal${i}">--.-°C</span>
      </div>`;
    chipContainer.innerHTML += `
      <div class="sensor-bar-row">
        <span class="bar-label">Chip-${i}</span>
        <div class="bar-track"><div class="bar-fill" id="chipFill${i}"></div></div>
        <span class="bar-val" id="chipVal${i}">--.-°C</span>
      </div>`;
  }

  let pollIntervalMs = 1000;
  let pollTimer = null;

  // Polling Loop with Adaptive Server-side Interval
  async function fetchStatus() {
    try {
      const res = await fetch("/api/status");
      if (res.status === 401) {
        window.location.reload();
        return;
      }
      const data = await res.json();
      updateUI(data);

      if (data.interval) {
        pollIntervalMs = Math.max(500, data.interval * 1000);
      }
    } catch (e) {
      document.getElementById("statusBadge").className = "badge disconnected";
      document.getElementById("statusBadge").innerText = "[ DISCONNECTED ]";
    } finally {
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = setTimeout(fetchStatus, pollIntervalMs);
    }
  }

  function updateUI(d) {
    if (d.now_epoch) currentServerNow = d.now_epoch;
    else currentServerNow = Date.now() / 1000;

    // 1. Badge & Footer
    const badge = document.getElementById("statusBadge");
    if (d.miner_online) {
      if (d.miner_recovering) {
        badge.className = "badge";
        badge.innerText = "[ RETRYING: 10.8.1.86 ]";
      } else {
        badge.className = "badge connected";
        badge.innerText = "[ CONNECTED: 10.8.1.86 ]";
      }
    } else {
      badge.className = "badge disconnected";
      badge.innerText = "[ MINER ERROR ]";
    }
    document.getElementById("footerUpdated").innerText = `Last update: ${d.timestamp} (${d.latency_ms}ms)`;

    // 2. Metric Cards
    const ghs = d.hashrate_ghs !== undefined ? d.hashrate_ghs : (d.hashrate_ths || 0) * 1000;
    document.getElementById("valHashrate").innerText = `${ghs.toLocaleString('en-US', {minimumFractionDigits: 1, maximumFractionDigits: 1})} GH/s`;
    document.getElementById("valAccepted").innerText = d.accepted_shares.toLocaleString();
    document.getElementById("valRejected").innerText = d.rejected_shares.toLocaleString();
    document.getElementById("valRejectRatio").innerText = `Ratio: ${d.reject_ratio.toFixed(2)} %`;

    // Plug Card
    const pBtn = document.getElementById("plugSwitchBtn");
    if (d.plug && d.plug.online) {
      document.getElementById("valPlugPower").innerText = `${Math.round(d.plug.power_w)} W`;
      document.getElementById("valPlugLoad").innerText = `Load: ${(d.plug.power_w/1000).toFixed(2)} kW`;
      currentPlugState = d.plug.switch_on;
      if (d.plug.switch_on) {
        pBtn.className = "btn-switch-on";
        pBtn.innerText = "ON";
        document.getElementById("valPlugTemp").innerText = `Internal Temp: ${d.plug.plug_temp}°C`;
      } else {
        pBtn.className = "btn-switch-off";
        pBtn.innerText = "OFF";
        document.getElementById("valPlugTemp").innerText = `Power Cut Off`;
      }
    } else {
      document.getElementById("valPlugPower").innerText = "OFFLINE";
      pBtn.className = "btn-switch-off";
      pBtn.innerText = "OFFLINE";
    }

    // 3. Water Banner
    const dtSign = d.water_delta > 0 ? `+${d.water_delta.toFixed(1)}` : `${d.water_delta.toFixed(1)}`;
    latestWaterBannerText = `Inlet: ${d.water_inlet.toFixed(1)}°C | Outlet: ${d.water_outlet.toFixed(1)}°C (ΔT: ${dtSign}°C)`;
    if (hoverX === null) {
      document.getElementById("waterBanner").innerText = latestWaterBannerText;
    }

    // 4. Protection & Alerts
    const alertBox = document.getElementById("alertBanner");
    const rstBtn = document.getElementById("resetLockBtn");

    if (d.cutoff_triggered) {
      alertBox.innerText = "EMERGENCY CUTOFF ACTIVE: Plug cut off.\\nManual reset required.";
      alertBox.style.display = "block";
      rstBtn.style.display = "block";
    } else if (d.overheat_wrn_seconds > 0) {
      const rem = Math.max(0, 30 - d.overheat_wrn_seconds);
      alertBox.innerText = `WRN TEMP: ${d.chip_max.toFixed(1)}°C >= ${d.wrn_temp}°C! Cutoff in ${rem}s`;
      alertBox.style.display = "block";
      rstBtn.style.display = "none";
    } else {
      alertBox.style.display = "none";
      rstBtn.style.display = "none";
    }

    // 5. Sync Server-Authoritative Settings across all viewing devices
    // (Focus-aware: do not interrupt user if actively editing that specific input)
    const wrnInput = document.getElementById("wrnSpin");
    if (document.activeElement !== wrnInput && d.wrn_temp !== undefined) {
      wrnInput.value = d.wrn_temp;
    }

    const stopInput = document.getElementById("stopSpin");
    if (document.activeElement !== stopInput && d.stop_temp !== undefined) {
      stopInput.value = d.stop_temp;
    }

    const intervalInput = document.getElementById("intervalInput");
    if (document.activeElement !== intervalInput && d.interval !== undefined) {
      intervalInput.value = d.interval;
    }

    const pauseBtn = document.getElementById("pauseBtn");
    if (d.paused !== undefined) {
      isPaused = d.paused;
      pauseBtn.innerText = isPaused ? "Resume" : "Pause";
    }

    // 6. Sensor Bars
    const wrn = d.wrn_temp;
    for (let i = 0; i < 6; i++) {
      const pVal = d.pcb_temps[i] || 0;
      const cVal = d.chip_temps[i] || 0;

      updateBar("pcb", i + 1, pVal, 80, wrn);
      updateBar("chip", i + 1, cVal, 85, wrn);
    }

    // 7. Curves data
    if (d.curves) {
      curvesData = d.curves;
      drawCanvasPlot();
    }
  }

  function updateBar(type, idx, val, maxRange, wrn) {
    const fill = document.getElementById(`${type}Fill${idx}`);
    const text = document.getElementById(`${type}Val${idx}`);
    const pct = Math.min(100, Math.max(0, (val / maxRange) * 100));
    fill.style.width = `${pct}%`;
    text.innerText = `${val.toFixed(1)}°C`;

    let color = "#00c49f";
    if (val >= wrn || val >= 78) color = "#e76f51";
    else if (val >= 68) color = "#f4a261";
    else if (val >= 55) color = "#00a8e8";

    fill.style.backgroundColor = color;
    text.style.color = color;
  }

  // --- High-DPI HTML5 Canvas Chart with Full Auto-Resize & Zoom/Pan ---
  const canvas = document.getElementById("plotCanvas");
  const ctx = canvas.getContext("2d");
  const wrapper = document.getElementById("plotWrapper");

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = wrapper.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;

    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    ctx.resetTransform();
    ctx.scale(dpr, dpr);
    drawCanvasPlot();
  }

  // Use ResizeObserver for instant responsive auto-scaling when window/panel resizes
  const ro = new ResizeObserver(() => {
    resizeCanvas();
  });
  ro.observe(wrapper);

  function drawCanvasPlot() {
    const w = wrapper.clientWidth;
    const h = wrapper.clientHeight;
    if (w <= 0 || h <= 0) return;

    ctx.clearRect(0, 0, w, h);

    const padL = 42, padR = 15, padT = 16, padB = 24;
    const plotW = w - padL - padR;
    const plotH = h - padT - padB;
    if (plotW <= 0 || plotH <= 0) return;

    // Viewport Time Bounds (Epoch Timestamps)
    const viewRight = currentServerNow - viewOffset;
    const viewLeft = viewRight - viewSpan;

    // Update zoom hint
    const hint = document.getElementById("zoomHint");
    if (viewSpan < 3550 || viewOffset > 10) {
      hint.innerText = `[ Zoom: ${(3600/viewSpan).toFixed(1)}x | Span: ${Math.round(viewSpan/60)}m | DblClick to reset ]`;
      hint.style.color = "#ffd166";
    } else {
      hint.innerText = `(Scroll to zoom, drag to pan, dblclick to reset)`;
      hint.style.color = "#868c9c";
    }

    // Y Range Adaptive based on visible points in current viewport
    let allVals = [];
    const n = curvesData.x ? curvesData.x.length : 0;
    for (let i = 0; i < n; i++) {
      const tx = curvesData.x[i];
      if (tx >= viewLeft && tx <= viewRight) {
        if (curvesData.inlet[i]) allVals.push(curvesData.inlet[i]);
        if (curvesData.outlet[i]) allVals.push(curvesData.outlet[i]);
        if (curvesData.chip_max[i]) allVals.push(curvesData.chip_max[i]);
        if (curvesData.chip_avg[i]) allVals.push(curvesData.chip_avg[i]);
      }
    }
    allVals = allVals.filter(v => v > 0);

    let minY = 35, maxY = 75;
    if (allVals.length > 0) {
      const minA = Math.min(...allVals);
      const maxA = Math.max(...allVals);
      if (maxA - minA < 10) {
        const mid = (minA + maxA) / 2;
        minY = Math.max(0, mid - 6);
        maxY = mid + 6;
      } else {
        minY = Math.max(0, minA - 2.5);
        maxY = maxA + 3.0;
      }
    }

    // Grid & Y Ticks
    ctx.strokeStyle = "#242731";
    ctx.lineWidth = 1;
    ctx.fillStyle = "#868c9c";
    ctx.font = "10px Consolas, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    const yStep = (maxY - minY) / 4;
    for (let i = 0; i <= 4; i++) {
      const yVal = minY + yStep * i;
      const py = padT + plotH - (plotH * (yVal - minY) / (maxY - minY));
      ctx.beginPath();
      ctx.moveTo(padL, py);
      ctx.lineTo(padL + plotW, py);
      ctx.stroke();
      ctx.fillText(`${yVal.toFixed(0)}°`, padL - 6, py);
    }

    // Dynamic X Grid & Ticks (Local Wall-Clock Time: HH:MM / HH:MM:SS)
    ctx.textAlign = "center";
    ctx.textBaseline = "top";

    const tickCount = 5;
    const tickStepSec = viewSpan / tickCount;
    const needSec = (viewSpan <= 300); // 视口跨度 <= 5分钟时显示秒
    for (let i = 0; i <= tickCount; i++) {
      const curEpoch = viewLeft + tickStepSec * i;
      const px = padL + plotW * ((curEpoch - viewLeft) / viewSpan);

      ctx.beginPath();
      ctx.moveTo(px, padT);
      ctx.lineTo(px, padT + plotH);
      ctx.stroke();

      const lbl = formatClock(curEpoch, needSec);
      ctx.fillText(lbl, px, padT + plotH + 6);
    }

    // Outer Plot Border
    ctx.strokeStyle = "#2d313b";
    ctx.strokeRect(padL, padT, plotW, plotH);

    // Map X / Y
    function getX(sec) {
      return padL + plotW * ((sec - viewLeft) / viewSpan);
    }
    function getY(temp) {
      return padT + plotH - (plotH * Math.min(1.0, Math.max(0.0, (temp - minY) / (maxY - minY))));
    }

    // Draw 4 Smooth Curves
    if (n > 0) {
      ctx.save();
      // Clip to plot area so lines don't bleed outside during zoom/pan
      ctx.beginPath();
      ctx.rect(padL, padT, plotW, plotH);
      ctx.clip();

      drawCurve(curvesData.inlet, "#00a8e8", 2.0);
      drawCurve(curvesData.outlet, "#f4a261", 2.0);
      drawCurve(curvesData.chip_max, "#e76f51", 2.0);
      drawCurve(curvesData.chip_avg, "#e67e22", 1.5, true);

      ctx.restore();
    }

    function drawCurve(series, color, lineWidth, isDash = false) {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = lineWidth;
      if (isDash) ctx.setLineDash([4, 3]);
      ctx.beginPath();
      let started = false;
      const maxGapSec = 15; // 采样时间差 > 15s 视为停机空档，断开连线！
      for (let i = 0; i < n; i++) {
        const tx = curvesData.x[i];
        if (tx < viewLeft - 30 || tx > viewRight + 30) continue;
        const px = getX(tx);
        const py = getY(series[i]);
        if (!started || (i > 0 && Math.abs(curvesData.x[i] - curvesData.x[i - 1]) > maxGapSec)) {
          ctx.moveTo(px, py);
          started = true;
        } else {
          ctx.lineTo(px, py);
        }
      }
      ctx.stroke();
      ctx.restore();
    }

    // Interactive Hover Crosshairs & Top-Right Badges
    if (hoverX !== null && n > 0 && hoverX >= padL && hoverX <= padL + plotW) {
      const cursorSec = viewLeft + ((hoverX - padL) / plotW) * viewSpan;
      let closestIdx = -1;
      let minDiff = Infinity;
      for (let i = 0; i < n; i++) {
        const diff = Math.abs(curvesData.x[i] - cursorSec);
        if (diff < minDiff) {
          minDiff = diff;
          closestIdx = i;
        }
      }

      if (closestIdx >= 0) {
        const snapPx = getX(curvesData.x[closestIdx]);

        // 1. Vertical Line
        ctx.save();
        ctx.strokeStyle = "#8890a0";
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(snapPx, padT);
        ctx.lineTo(snapPx, padT + plotH);
        ctx.stroke();
        ctx.restore();

        // 2. Crosshairs & Top-Right Badges on 4 curves
        const items = [
          { val: curvesData.inlet[closestIdx], col: "#00a8e8" },
          { val: curvesData.outlet[closestIdx], col: "#f4a261" },
          { val: curvesData.chip_max[closestIdx], col: "#e76f51" },
          { val: curvesData.chip_avg[closestIdx], col: "#e67e22" },
        ];

        // Top Banner shows authentic wall-clock time
        const snapClock = formatClock(curvesData.x[closestIdx], true);
        document.getElementById("waterBanner").innerText = `[${snapClock}] In:${curvesData.inlet[closestIdx].toFixed(1)}°C | Out:${curvesData.outlet[closestIdx].toFixed(1)}°C | ChipMax:${curvesData.chip_max[closestIdx].toFixed(1)}°C | ChipAvg:${curvesData.chip_avg[closestIdx].toFixed(1)}°C`;

        items.forEach(item => {
          if (!item.val) return;
          const py = getY(item.val);

          // '+' Crosshair
          ctx.strokeStyle = item.col;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(snapPx - 6, py); ctx.lineTo(snapPx + 6, py);
          ctx.moveTo(snapPx, py - 6); ctx.lineTo(snapPx, py + 6);
          ctx.stroke();

          // Top-Right Badge (Strictly Top-Right to prevent line overlap)
          const text = `${item.val.toFixed(1)}°C`;
          ctx.font = "bold 11px Consolas, monospace";
          const tw = ctx.measureText(text).width;
          const th = 14;

          let bx = snapPx + 5;
          let by = py - th - 3;
          if (bx + tw + 6 > padL + plotW) bx = snapPx - tw - 10; // Flip near edge
          if (by < padT) by = py + 5; // Flip bottom if at top edge

          ctx.fillStyle = "rgba(24, 26, 32, 0.92)";
          ctx.fillRect(bx, by, tw + 6, th);
          ctx.strokeStyle = item.col;
          ctx.lineWidth = 1;
          ctx.strokeRect(bx, by, tw + 6, th);

          ctx.fillStyle = item.col;
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          ctx.fillText(text, bx + 3, by + th / 2);
        });
      }
    }
  }

  // --- Zoom, Pan & Hover Event Listeners ---
  wrapper.addEventListener("mousemove", e => {
    const rect = canvas.getBoundingClientRect();
    hoverX = e.clientX - rect.left;
    lastMouseMoveTime = Date.now();

    if (isDragging) {
      const dx = e.clientX - dragStartX;
      const plotW = wrapper.clientWidth - 42 - 15;
      const dt = (dx / plotW) * viewSpan;
      viewOffset = Math.max(0, Math.min(3600 - viewSpan, dragStartOffset + dt));
    }

    drawCanvasPlot();
  });

  wrapper.addEventListener("mousedown", e => {
    if (e.button === 0) {
      isDragging = true;
      dragStartX = e.clientX;
      dragStartOffset = viewOffset;
    }
  });

  window.addEventListener("mouseup", () => {
    isDragging = false;
  });

  let latestWaterBannerText = "Inlet: --.-°C | Outlet: --.-°C (ΔT: --.-°C)";

  wrapper.addEventListener("mouseleave", () => {
    hoverX = null;
    isDragging = false;
    document.getElementById("waterBanner").innerText = latestWaterBannerText;
    drawCanvasPlot();
  });

  // Mouse Wheel Zoom In / Out
  wrapper.addEventListener("wheel", e => {
    e.preventDefault();
    lastMouseMoveTime = Date.now();
    const zoomFactor = e.deltaY < 0 ? 0.75 : 1.33;
    viewSpan = Math.max(60, Math.min(3600, viewSpan * zoomFactor));
    viewOffset = Math.max(0, Math.min(3600 - viewSpan, viewOffset));
    drawCanvasPlot();
  }, { passive: false });

  // Double click resets zoom to full 1 hour
  wrapper.addEventListener("dblclick", () => {
    viewSpan = 3600;
    viewOffset = 0;
    drawCanvasPlot();
  });

  // Auto right-align reset after 6s of no mouse interaction
  setInterval(() => {
    if (Date.now() - lastMouseMoveTime > 6000) {
      if (hoverX !== null || viewSpan !== 3600 || viewOffset !== 0) {
        hoverX = null;
        viewSpan = 3600;
        viewOffset = 0;
        drawCanvasPlot();
      }
    }
  }, 1000);

  // Settings & Buttons Event Listeners
  document.getElementById("intervalInput").addEventListener("change", async e => {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ interval: parseFloat(e.target.value) })
    });
  });

  document.getElementById("wrnSpin").addEventListener("change", async e => {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wrn_temp: parseFloat(e.target.value) })
    });
  });

  document.getElementById("stopSpin").addEventListener("change", async e => {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stop_temp: parseFloat(e.target.value) })
    });
  });

  document.getElementById("pauseBtn").addEventListener("click", async () => {
    isPaused = !isPaused;
    document.getElementById("pauseBtn").innerText = isPaused ? "Resume" : "Pause";
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused: isPaused })
    });
  });

  document.getElementById("clearChartBtn").addEventListener("click", async () => {
    await fetch("/api/clear_history", { method: "POST" });
    curvesData = { x: [], inlet: [], outlet: [], chip_max: [], chip_avg: [] };
    hoverX = null;
    drawCanvasPlot();
  });

  document.getElementById("resetLockBtn").addEventListener("click", async () => {
    await fetch("/api/reset_lock", { method: "POST" });
  });

  // Plug Switch & Password Modal
  const modal = document.getElementById("pwdModal");
  const pwdInput = document.getElementById("pwdInput");
  const pwdErr = document.getElementById("pwdErr");

  document.getElementById("plugSwitchBtn").addEventListener("click", async () => {
    if (currentPlugState) {
      await fetch("/api/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state: false })
      });
    } else {
      pwdErr.style.display = "none";
      pwdInput.value = "";
      modal.style.display = "flex";
      pwdInput.focus();
    }
  });

  document.getElementById("pwdConfirmBtn").addEventListener("click", async () => {
    const pwd = pwdInput.value;
    try {
      const res = await fetch("/api/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state: true, password: pwd })
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        modal.style.display = "none";
      } else {
        pwdErr.style.display = "block";
        pwdInput.select();
      }
    } catch (e) {
      pwdErr.style.display = "block";
      pwdInput.select();
    }
  });

  pwdInput.addEventListener("keydown", e => {
    if (e.key === "Enter") {
      document.getElementById("pwdConfirmBtn").click();
    } else if (e.key === "Escape") {
      modal.style.display = "none";
    }
  });

  document.getElementById("pwdCancelBtn").addEventListener("click", () => {
    modal.style.display = "none";
  });

  // Initial setup & start
  resizeCanvas();
  fetchStatus();
</script>
</body>
</html>
"""


CONFIG_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(config_path: str = CONFIG_DEFAULT_PATH) -> Dict[str, str]:
    """Load or create config.json for login_password and power_password."""
    default_config = {
        "login_password": "antminer",
        "power_password": "dl.general"
    }
    if not os.path.exists(config_path):
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except Exception as e:
            sys.stderr.write(f"[Config Warning] Failed to create {config_path}: {e}\n")
        return default_config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            return {
                "login_password": str(cfg.get("login_password", default_config["login_password"])),
                "power_password": str(cfg.get("power_password", default_config["power_password"])),
            }
    except Exception as e:
        sys.stderr.write(f"[Config Warning] Failed to parse {config_path}: {e}. Using defaults.\n")
        return default_config


class AntMinerWebHandler(BaseHTTPRequestHandler):
    """Threading HTTP Handler serving Single Page App and REST APIs with Password-only Auth."""

    def log_message(self, format, *args):
        pass

    def check_basic_auth(self) -> bool:
        """Verify HTTP Authentication: Password only, username is not required."""
        if not getattr(self.server, "auth_enabled", True):
            return True

        auth_header = self.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            self.send_auth_challenge()
            return False

        try:
            encoded_creds = auth_header[6:].strip()
            decoded = base64.b64decode(encoded_creds).decode("utf-8", errors="ignore")
            if ":" in decoded:
                _, password = decoded.split(":", 1)
            else:
                password = decoded

            expected_pass = getattr(self.server, "login_password", "antminer")
            if hmac.compare_digest(password, expected_pass):
                return True
        except Exception:
            pass

        self.send_auth_challenge()
        return False

    def send_auth_challenge(self):
        """Send 401 Unauthorized response with WWW-Authenticate header."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AntMinerTab Dashboard (Password Only)", charset="UTF-8"')
        if self.path.startswith("/api/"):
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"Unauthorized","msg":"Password authentication required"}')
        else:
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = (
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<title>401 Unauthorized - AntMinerTab</title>'
                '<style>'
                'body{background:#131417;color:#e0e2e8;font-family:Consolas,monospace;'
                'display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}'
                '.box{border:1px solid #572635;background:#2b171c;padding:32px 48px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.5);}'
                'h2{color:#e76f51;font-size:20px;margin-bottom:12px;letter-spacing:1px;}'
                'p{color:#868c9c;font-size:13px;line-height:1.6;}'
                '</style></head>'
                '<body><div class="box">'
                '<h2>[ 401 UNAUTHORIZED ]</h2>'
                '<p>Authentication Required: Please enter your login password.<br>'
                '(Username is not required / ignored)<br>'
                'AntMinerTab Industrial Monitoring Console</p>'
                '</div></body></html>'
            )
            self.wfile.write(html.encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        if not self.check_basic_auth():
            return

        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif parsed.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            payload = self.server.state_manager.get_api_payload()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self.check_basic_auth():
            return

        parsed = urlparse(self.path)
        try:
            content_len_str = self.headers.get("Content-Length", "0")
            length = int(content_len_str)
            if length < 0 or length > 1048576:  # Max 1MB payload
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            req_data = json.loads(body)
        except Exception:
            req_data = {}

        if parsed.path == "/api/switch":
            state = req_data.get("state", False)
            pwd = str(req_data.get("password", ""))
            if state:
                expected_power_pwd = str(getattr(self.server, "power_password", "dl.general"))
                if not hmac.compare_digest(pwd, expected_power_pwd):
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":false,"msg":"Invalid password"}')
                    return

            res = self.server.state_manager.set_plug_switch(bool(state))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": res, "state": state}).encode("utf-8"))

        elif parsed.path == "/api/settings":
            self.server.state_manager.set_settings(
                interval=req_data.get("interval"),
                wrn=req_data.get("wrn_temp"),
                stop=req_data.get("stop_temp"),
                paused=req_data.get("paused"),
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        elif parsed.path == "/api/reset_lock":
            self.server.state_manager.reset_protection_lock()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        elif parsed.path == "/api/clear_history":
            self.server.state_manager.clear_history()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="AntMinerTab Web Server Edition (Port 20000)")
    parser.add_argument("--host", default=os.getenv("ANTMINER_HOST", "0.0.0.0"), help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("ANTMINER_PORT", "20000")), help="Port to listen on (default: 20000)")
    parser.add_argument("--config", default=CONFIG_DEFAULT_PATH, help=f"Path to configuration file (default: {CONFIG_DEFAULT_PATH})")

    args = parser.parse_args()

    # Load configuration from local config file
    config = load_config(args.config)
    login_password = config["login_password"]
    power_password = config["power_password"]

    print("=" * 72)
    print(" 🚀 AntMinerTab Web Server Starting...")
    print(f" Target Miner: http://10.8.1.86")
    print(f" Target Plug:  10.8.1.110 (Mijia Smart Plug 3)")
    print(f" Telegram Bot: @s332854BOT (7775553661)")
    print("=" * 72)
    print(f" Config File:   {os.path.basename(args.config)} (Loaded)")
    print(f" Web Security:  Password Protection ENABLED (Default)")
    print(f" Auth Mode:     Password Only (Username is ignored / not required)")
    print("=" * 72)
    print(f" Local Access:     http://127.0.0.1:{args.port}")
    print(f" Tailscale Access: http://<tailscale-ip>:{args.port}")
    print(f" LAN Access:       http://<host-ip>:{args.port}")
    print("=" * 72)

    state_mgr = MonitorStateManager()

    server = ThreadingHTTPServer((args.host, args.port), AntMinerWebHandler)
    server.state_manager = state_mgr
    server.auth_enabled = True
    server.login_password = login_password
    server.power_password = power_password

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping AntMinerTab Web Server...")
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
