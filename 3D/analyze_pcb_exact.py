import zipfile
import json

path = r'D:\Document\tempPrj\AntMiner\CoolerHD\水冷机温控器.epro2'
with zipfile.ZipFile(path, 'r') as z:
    with z.open('多通道PWM调速器.epru') as f:
        data = f.read().decode('utf-8', errors='ignore')

# 1. 查找板框
for line in data.split('\n'):
    if '\"layerId\":11' in line and 'POLY' in line:
        print("Board Outline:", line[:200])

# 2. 查找元器件
components = []
for line in data.split('\n'):
    if '\"type\":\"COMPONENT\"' in line:
        parts = line.split('||')
        if len(parts) >= 2:
            try:
                b = json.loads(parts[1].rstrip('|'))
                pid = b.get('partId', '')
                x_mil = b.get('x', 0)
                y_mil = b.get('y', 0)
                rot = b.get('rotation', 0)
                components.append((pid, x_mil / 39.3701, y_mil / 39.3701, rot))
            except: pass

print(f"Total components: {len(components)}")

# 打印我们要关注的器件
targets = {
    '744-81-04TB1B.1': 'Fan 4Pin',
    'WJ45C-B-2P.1': 'Power Terminal',
    'HX TYPE-BF 90.1': 'USB Type-B',
    'Key_SMD_3x 4x2.1': 'Key Button',
    'LED_R.1': 'LED Indicator',
    'EKM2GM330G18OTBVZC.1': 'Capacitor',
    'STM32F103C8T6.1': 'MCU'
}

for c in components:
    for t_k, t_name in targets.items():
        if t_k in c[0]:
            print(f"{t_name:<18} ({c[0]:<20}): X = {c[1]:6.2f} mm, Y = {c[2]:6.2f} mm, Rot = {c[3]} deg")

