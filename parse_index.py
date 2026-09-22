import os
import struct

index_file = r"D:\Document\tempPrj\BITMAIN-S19Hyd-151T-Firmware\.git\index"
result_file = r"D:\Document\tempPrj\BITMAIN-S19Hyd-151T-Firmware\check_result.txt"

with open(index_file, "rb") as f:
    data = f.read()

# Git index binary format:
# 4 bytes signature b'DIRC'
# 4 bytes version
# 4 bytes number of index entries
header = data[:12]
sig, ver, entries = struct.unpack(">4sII", header)

tracked_files = []
offset = 12
for _ in range(entries):
    # Entry header is 62 bytes (stat info + sha + flags)
    # flags is 2 bytes at offset + 60
    flags = struct.unpack(">H", data[offset+60:offset+62])[0]
    name_len = flags & 0xFFF
    name_start = offset + 62
    name_end = data.find(b"\x00", name_start)
    name = data[name_start:name_end].decode("utf-8", errors="replace")
    tracked_files.append(name)
    entry_len = ((62 + (name_end - name_start) + 8) // 8) * 8
    offset += entry_len

is_config_tracked = any("config.json" in f for f in tracked_files)
with open(result_file, "w", encoding="utf-8") as out:
    out.write(f"Total entries: {entries}\n")
    out.write(f"config.json tracked: {is_config_tracked}\n")
    for f in tracked_files:
        if "WebMonitor" in f or "config" in f:
            out.write(f"Tracked: {f}\n")
