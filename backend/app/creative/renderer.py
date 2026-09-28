from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from app.core.config import ROOT
from app.core.errors import DomainError
from app.schemas.domain import Slide, Visual

FONT = ROOT / "assets/fonts/Manrope.ttf"
WIDTH, HEIGHT, MARGIN = 1080, 1350, 84


def font(size):
    if not FONT.exists():
        raise DomainError("Renderer font missing. Run setup to install assets/fonts/Manrope.ttf")
    return ImageFont.truetype(str(FONT), size)


def fit_text(draw, value, box, maximum=60, minimum=26, color="#183C36"):
    x, y, w, h = box
    for size in range(maximum, minimum - 1, -2):
        face, lines = font(size), []
        valid = True
        for paragraph in value.split("\n"):
            line = ""
            for word in paragraph.split():
                if draw.textlength(word, font=face) > w:
                    valid = False
                    break
                candidate = (line + " " + word).strip()
                if draw.textlength(candidate, font=face) > w:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        spacing = int(size * 1.4)
        if valid and len(lines) * spacing <= h:
            for i, line in enumerate(lines):
                draw.text((x, y + i * spacing), line, font=face, fill=color, anchor="lt")
            return len(lines) * spacing
    raise DomainError("Slide text cannot fit safely. Shorten text or split it into more slides.")


def render_slide(slide: Slide, visual: Visual, index=1, total=1):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), visual.background)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((84, 68, 996, 76), radius=4, fill=visual.accent)
    fit_text(draw, visual.mark, (84, 104, 780, 70), 24, 18, visual.muted)
    title_y = 260 if slide.kind in {"cover", "end"} else 225
    used = fit_text(draw, slide.title, (84, title_y, 912, 330), 86 if slide.kind == "cover" else 66, 38, visual.foreground)
    y = max(540, title_y + used + 60)
    bottom = 1160
    if slide.kind in {"two_column", "do_dont"}:
        values = slide.items or slide.body.split("\n\n")
        if len(values) < 2:
            raise DomainError("Two-column templates require at least two items")
        middle = (len(values) + 1) // 2
        for col, group in enumerate((values[:middle], values[middle:])):
            x = 84 + col * 478
            draw.rounded_rectangle((x, y, x + 434, bottom), radius=24, fill=visual.accent if col == 0 else visual.foreground)
            label = ("DO" if col == 0 else "AVOID") + "\n\n" if slide.kind == "do_dont" else ""
            fit_text(draw, label + "\n\n".join(group), (x + 28, y + 30, 378, bottom - y - 60), 38, 26, visual.foreground if col == 0 else visual.background)
    elif slide.kind in {"checklist", "steps"}:
        values = slide.items or slide.body.splitlines()
        if not values:
            raise DomainError("Checklist and step slides require items")
        row_height = (bottom - y) // len(values)
        if row_height < 58:
            raise DomainError("Too many items on slide")
        for i, value in enumerate(values):
            cy = y + i * row_height
            draw.rounded_rectangle((84, cy + 3, 132, cy + 51), radius=12, fill=visual.accent)
            draw.text((98, cy + 11), str(i + 1) if slide.kind == "steps" else "+", font=font(24), fill=visual.foreground)
            fit_text(draw, value, (158, cy, 838, row_height - 14), 42, 26, visual.foreground)
    else:
        text = slide.body + ("\n\n" if slide.body and slide.items else "") + "\n".join(slide.items)
        if slide.kind == "tip":
            draw.rounded_rectangle((64, y - 24, 1016, bottom + 16), radius=32, fill=visual.accent)
        fit_text(draw, text, (84, y, 912, bottom - y), 46, 28, visual.foreground)
    draw.line((84, 1220, 996, 1220), fill=visual.muted, width=1)
    draw.text((84, 1254), "PRACTICAL NOTES" if visual.mark == "DFB / FIELD NOTES" else visual.mark[:35], font=font(20), fill=visual.muted)
    draw.text((996, 1254), f"{index:02d} / {total:02d}", font=font(22), fill=visual.foreground, anchor="rt")
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
