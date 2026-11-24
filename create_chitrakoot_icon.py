"""
Converts the generated Chitrakoot logo into:
1. static/chitrakoot.png (high-res web asset)
2. static/chitrakoot.ico (multi-resolution Windows icon file)
"""

import os
import sys
from PIL import Image

def convert_logo(source_image_path, target_dir):
    os.makedirs(target_dir, exist_ok=True)
    
    png_target = os.path.join(target_dir, "chitrakoot.png")
    ico_target = os.path.join(target_dir, "chitrakoot.ico")
    
    img = Image.open(source_image_path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
        
    # Save standard PNG
    img.save(png_target, "PNG")
    print(f"Saved PNG to {png_target}")
    
    # Save multi-res Windows ICO
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(ico_target, format="ICO", sizes=sizes)
    print(f"Saved multi-res ICO to {ico_target}")
    return png_target, ico_target

if __name__ == "__main__":
    if len(sys.argv) > 1:
        src = sys.argv[1]
        out_dir = sys.argv[2] if len(sys.argv) > 2 else "C:\\jarvis\\gemini_live_vision\\static"
        convert_logo(src, out_dir)
    else:
        print("Usage: python create_chitrakoot_icon.py <source_image_path> [target_dir]")
