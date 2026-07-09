from PIL import Image, ImageDraw, ImageFont
import os

# 创建 1024x1024 图标
size = 1024
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# 绘制圆角矩形背景（橙色渐变效果）
for y in range(size):
    r = 255
    g = int(100 + (y / size) * 50)
    b = int(0 + (y / size) * 30)
    draw.line([(0, y), (size, y)], fill=(r, g, b, 255))

# 绘制内部圆角矩形边框
draw.rounded_rectangle([40, 40, size-40, size-40], radius=200, outline=(255, 255, 255, 100), width=8)

# 添加文字 "可乐口播" 2x2 布局
try:
    font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 200)
except:
    font = ImageFont.load_default()

line1 = "可乐"
line2 = "口播"
bbox1 = draw.textbbox((0, 0), line1, font=font)
tw1 = bbox1[2] - bbox1[0]
th1 = bbox1[3] - bbox1[1]
bbox2 = draw.textbbox((0, 0), line2, font=font)
tw2 = bbox2[2] - bbox2[0]
th2 = bbox2[3] - bbox2[1]

# 居中绘制两行文字
x1 = (size - tw1) // 2
y1 = size // 2 - th1 - 20
x2 = (size - tw2) // 2
y2 = size // 2 + 20

draw.text((x1, y1), line1, font=font, fill=(255, 255, 255, 255))
draw.text((x2, y2), line2, font=font, fill=(255, 255, 255, 255))

# 保存为 PNG
out_path = "/Users/zxz/Documents/trae_projects/sop-agents/desktop-app/可乐视频生成器_Mac版_v1.0/app_icon.png"
img.save(out_path)
print(f"图标已保存: {out_path}")
print(f"尺寸: {img.size}")
