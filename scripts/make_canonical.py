from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures/canonical.png"


def font(size: int):
    for candidate in (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/arial.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


image = Image.new("RGB", (438, 438), "#000000")
draw = ImageDraw.Draw(image)
draw.text((219, 177), "10:08", font=font(92), fill="#FFFFFF", anchor="mm")
draw.text((219, 274), "08.20", font=font(22), fill="#9EA4AD", anchor="mm")
draw.text((219, 307), "THU", font=font(17), fill="#5E6672", anchor="mm")
OUT.parent.mkdir(parents=True, exist_ok=True)
image.save(OUT)
print(OUT)

