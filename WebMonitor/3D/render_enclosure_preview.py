import trimesh
import numpy as np
from PIL import Image, ImageDraw
import os

out_dir = r'D:\Document\tempPrj\AntMiner\3D'
bot_step = os.path.join(out_dir, 'CoolerHD_Enclosure_Bottom.step')
top_step = os.path.join(out_dir, 'CoolerHD_Enclosure_Top.step')

# Load STEP using trimesh (trimesh handles STEP if cascadepro/opencascade or mesh export)
# Let's check if trimesh can load STEP or export to STL first
print("Script started")
