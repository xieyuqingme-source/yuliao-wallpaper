"""Small original procedural sample, created only after the user requests it."""
from pathlib import Path

from PIL import Image, ImageDraw


def create_demo(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1920, 1080))
    draw = ImageDraw.Draw(image)
    for y in range(1080):
        t = y / 1080
        draw.line((0, y, 1920, y), fill=(int(32 + 116 * t), int(75 + 92 * t), int(99 + 68 * t)))
    draw.ellipse((1330, 145, 1520, 335), fill=(243, 216, 167))
    draw.polygon([(0, 765), (210, 540), (405, 715), (760, 362), (1080, 688), (1390, 450), (1720, 709), (1920, 576), (1920, 1080), (0, 1080)], fill=(54, 102, 115))
    draw.polygon([(0, 912), (325, 714), (610, 900), (970, 675), (1360, 944), (1700, 755), (1920, 882), (1920, 1080), (0, 1080)], fill=(30, 77, 90))
    draw.polygon([(0, 1030), (390, 960), (700, 1030), (1050, 886), (1490, 1050), (1920, 948), (1920, 1080), (0, 1080)], fill=(19, 51, 65))
    image.save(path, format="PNG")
