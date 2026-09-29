"""Deterministic portrait graphics with explicit phone-readable text budgets."""

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


def fit_text(draw, value, box, maximum=60, minimum=38, color="#183C36"):
    if not value.strip():
        return 0
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
                    if line:
                        lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        spacing = int(size * 1.38)
        if valid and len(lines) * spacing <= h:
            for i, line in enumerate(lines):
                draw.text((x, y + i * spacing), line, font=face, fill=color, anchor="lt")
            return len(lines) * spacing
    raise DomainError("Slide text exceeds the phone-readable area. Shorten it or split the idea into slides.")


def validate_budget(slide):
    limits = {"cover": (115, 220), "numbered_action": (75, 210), "checklist": (78, 140),
              "steps": (78, 140), "statement": (110, 230), "end": (100, 230)}
    title_limit, body_limit = limits.get(slide.kind, (85, 190))
    if len(slide.title.strip()) > title_limit:
        raise DomainError(f"{slide.kind.replace('_', ' ').title()} title is too long for phone reading ({title_limit} characters max)")
    if len(slide.body.strip()) > body_limit:
        raise DomainError(f"{slide.kind.replace('_', ' ').title()} explanation is too long for phone reading ({body_limit} characters max)")
    if len(slide.items) > 4 or any(len(value.strip()) > 65 for value in slide.items):
        raise DomainError("Use at most four short list items of 65 characters each on one slide")


def _footer(draw, visual, index, total, inverse=False):
    ink = visual.background if inverse else visual.foreground
    faint = visual.background if inverse else visual.muted
    if visual.border_treatment == "line":
        draw.line((MARGIN, 1215, WIDTH - MARGIN, 1215), fill=faint, width=2)
    if visual.footer_treatment == "mark" and visual.mark:
        fit_text(draw, visual.mark, (MARGIN, 1242, 700, 52), 30, 26, faint)
    draw.text((WIDTH - MARGIN, 1245), f"{index:02d} / {total:02d}", font=font(25), fill=ink, anchor="rt")


def _items(draw, values, y, visual):
    row_height = 105 if len(values) > 3 else 128
    if y + len(values) * row_height > 1180:
        raise DomainError("List items exceed the safe slide area. Shorten them or use fewer items.")
    for number, value in enumerate(values, start=1):
        cy = y + (number - 1) * row_height
        draw.rounded_rectangle((MARGIN, cy + 8, MARGIN + 48, cy + 56), radius=14, fill=visual.accent)
        draw.text((MARGIN + 24, cy + 30), str(number), font=font(26), fill=visual.foreground, anchor="mm")
        fit_text(draw, value, (MARGIN + 76, cy, 825, row_height - 10), 44, 36, visual.foreground)


def render_slide(slide: Slide, visual: Visual, index=1, total=1, numbered=False):
    validate_budget(slide)
    inverse = slide.kind == "end"
    background = visual.foreground if inverse else visual.background
    ink = visual.background if inverse else visual.foreground
    canvas = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(canvas)
    radius = visual.corner_radius
    scale = visual.spacing_scale
    draw.rounded_rectangle((MARGIN, 68, WIDTH - MARGIN, 79), radius=5, fill=visual.accent)
    if numbered and index > 1 and slide.kind != "numbered_action":
        draw.rounded_rectangle((MARGIN, 123, MARGIN + 144, 194), radius=18, fill=visual.accent)
        draw.text((MARGIN + 72, 158), f"{index - 1:02d}", font=font(49),
                  fill=visual.foreground, anchor="mm")
    if slide.kind == "cover":
        draw.rounded_rectangle((MARGIN, 170, WIDTH - MARGIN, 1110), radius=radius + 12, fill=visual.foreground)
        eyebrow = visual.compact_mark or visual.eyebrow
        if eyebrow:
            label_width = min(500, max(104, int(draw.textlength(eyebrow, font=font(32))) + 48))
            draw.rounded_rectangle((MARGIN + 38, 205, MARGIN + 38 + label_width, 276),
                                   radius=20, fill=visual.accent)
            fit_text(draw, eyebrow, (MARGIN + 62, 222, label_width - 48, 44),
                     32, 27, visual.foreground)
        used = fit_text(draw, slide.title, (MARGIN + 45, 332, 822, 455), 91, 58, visual.background)
        fit_text(draw, slide.body, (MARGIN + 48, max(815, 332 + used + 35), 804, 230), 44, 36, visual.background)
        _footer(draw, visual, index, total)
    elif slide.kind == "numbered_action":
        draw.rounded_rectangle((MARGIN, 168, MARGIN + 187, 344), radius=radius, fill=visual.accent)
        draw.text((MARGIN + 92, 256), f"{index - 1:02d}", font=font(104), fill=visual.foreground, anchor="mm")
        used = fit_text(draw, slide.title, (MARGIN, 395, 910, 275), 78, 54, visual.foreground)
        y = max(700, 395 + used + int(36 * scale))
        body_used = fit_text(draw, slide.body, (MARGIN, y, 900, 275), 56, 43, visual.foreground)
        if slide.items:
            panel_y = max(930, y + body_used + 35)
            if panel_y + 58 * len(slide.items) > 1170:
                raise DomainError("Numbered action has too much explanation and list detail for one slide")
            draw.rounded_rectangle((MARGIN, panel_y - 18, WIDTH - MARGIN, 1185), radius=radius, fill=visual.accent)
            for n, value in enumerate(slide.items):
                fit_text(draw, "• " + value, (MARGIN + 36, panel_y + n * 57, 820, 55), 35, 32, visual.foreground)
        _footer(draw, visual, index, total)
    elif slide.kind in {"checklist", "steps"}:
        fit_text(draw, slide.title, (MARGIN, 203, 900, 250), 72, 52, ink)
        values = slide.items or [line.strip() for line in slide.body.splitlines() if line.strip()]
        if not values:
            raise DomainError("Checklist and step slides require short list items")
        if slide.items and slide.body.strip():
            fit_text(draw, slide.body, (MARGIN, 456, 900, 176), 52, 42, ink)
            _items(draw, values, 690, visual)
        else:
            _items(draw, values, 545, visual)
        _footer(draw, visual, index, total)
    elif slide.kind in {"statement", "tip", "end"}:
        draw.rounded_rectangle((MARGIN, 225, MARGIN + 22, 1080), radius=10, fill=visual.accent)
        used = fit_text(draw, slide.title, (MARGIN + 65, 300, 835, 380), 82, 54, ink)
        fit_text(draw, slide.body, (MARGIN + 65, max(730, 300 + used + 50), 830, 325), 54, 43, ink)
        if slide.items:
            raise DomainError("Statement slides should have one focused thought; move list items to a checklist")
        _footer(draw, visual, index, total, inverse=inverse)
    elif slide.kind in {"two_column", "do_dont"}:
        fit_text(draw, slide.title, (MARGIN, 195, 900, 240), 70, 50, ink)
        values = slide.items or [part.strip() for part in slide.body.split("\n\n") if part.strip()]
        if len(values) < 2:
            raise DomainError("Two-column slides require at least two short items")
        middle = (len(values) + 1) // 2
        for col, group in enumerate((values[:middle], values[middle:])):
            x = MARGIN + col * 468
            fill = visual.accent if col == 0 else visual.foreground
            draw.rounded_rectangle((x, 520, x + 438, 1125), radius=radius, fill=fill)
            prefix = ("DO\n" if col == 0 else "AVOID\n") if slide.kind == "do_dont" else ""
            fit_text(draw, prefix + "\n".join(group), (x + 30, 563, 380, 520), 44, 35,
                     visual.foreground if col == 0 else visual.background)
        _footer(draw, visual, index, total)
    else:
        fit_text(draw, slide.title, (MARGIN, 230, 900, 300), 74, 52, ink)
        fit_text(draw, slide.body + ("\n" + "\n".join(slide.items) if slide.items else ""),
                 (MARGIN, 620, 900, 500), 50, 38, ink)
        _footer(draw, visual, index, total)
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
