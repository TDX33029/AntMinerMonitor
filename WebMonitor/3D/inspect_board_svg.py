from PIL import Image
import os

# 查看从工程解压出来的 PCB 图片 (PCB1.png / 78d26620b95e2b37.png / b13052d005253b0d.png)
for f in os.listdir('.'):
    if f.endswith('.png') and len(f) > 20 and not f.startswith('CoolerHD') and not f.startswith('Delta'):
        print("Found EDA preview image:", f)
        im = Image.open(f)
        print("Image size:", im.size)

