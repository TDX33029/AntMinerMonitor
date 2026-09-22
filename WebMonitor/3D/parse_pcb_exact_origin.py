import zipfile
import json
import re

# 检查一下立创EDA中的图元与实际PCB各接口位置
path = r'D:\Document\tempPrj\AntMiner\CoolerHD\水冷机温控器.step'
# 在STEP文件中查找各组件的名字和位置
# Open CASCADE STEP processor
# 搜索产品定义
with open(path, 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read(500000) # 前500KB

# 找到包含 PRODUCT 的行
for line in text.split('\n'):
    if 'PRODUCT(' in line or 'PRODUCT_DEFINITION(' in line:
        if any(k in line.lower() for k in ['usb', '744', 'wj45', 'conn', 'led', 'key']):
            print(line[:150])

