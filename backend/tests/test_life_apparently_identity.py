"""Brand identity changes stay scoped to the existing brand and its renders."""

from io import BytesIO

from PIL import Image, ImageChops
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.brands.service import save_brand
from app.content import service as content_service
from app.creative import renderer
from app.creative import service as creative_service
from app.db.session import Base
from app.models import Brand, Content, Knowledge, Template
from app.schemas.domain import BrandConfig, BrandInput, EditorialPlan, GeneratedContent, GenerationInput, Slide
from scripts.update_life_help_editorial import update


def seeded_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    life = Brand(id=1, name="Life-help · working identity", slug="life_help", description="Old description",
                 config={"pillars": ["routines & weekly resets"], "editorial": {"description": "Old voice"},
                         "visual": {"background": "#F4F0E8", "foreground": "#183C36", "accent": "#D9F075",
                                    "muted": "#65726A", "mark": "PRACTICAL NOTES"},
                         "permissions": {"publish": "APPROVAL"}})
    other = Brand(id=2, name="ReemTeam", slug="reemteam", config={"pillars": ["routines & weekly resets"],
                        "editorial": {"description": "Reem voice"},
                        "visual": {"background": "#171D30", "foreground": "#F5F6FA", "accent": "#B5A5F5",
                                   "muted": "#A0A8C0", "mark": "REEMTEAM"}})
    db.add_all([life, other])
    db.flush()
    db.add_all([
        Template(brand_id=1, name="Cover", kind="cover", config={**life.config["visual"], "mark": "DFB / FIELD NOTES"}),
        Template(brand_id=1, name="Custom", kind="statement", config={**life.config["visual"], "accent": "#AABBCC"}),
        Template(brand_id=2, name="Cover", kind="cover", config=other.config["visual"]),
        Knowledge(brand_id=1, category="routines & weekly resets", title="Evening reset",
                  body="Prepare one thing tonight", source="Editorial", verification="APPROVED", enabled=True),
        Content(brand_id=1, topic="1 thing tonight", pillar="routines & weekly resets", format="carousel",
                hook="One useful step", slides=[{"kind": "cover", "title": "One useful step", "body": "", "items": []}],
                caption="Existing caption", knowledge_refs=[1], generation={"knowledge": [{"id": 1}]},
                status="REVIEW", revision=3),
    ])
    db.commit()
    return engine, db


def test_existing_brand_rename_is_idempotent_and_preserves_data_and_other_brand():
    engine, db = seeded_db()
    try:
        other_before = dict(db.get(Brand, 2).config)
        content_before = db.scalar(select(Content).where(Content.brand_id == 1))
        content_snapshot = (content_before.id, content_before.revision, content_before.slides,
                            content_before.knowledge_refs, content_before.generation)
        assert update(db) == (1, True)
        db.commit()
        assert update(db) == (1, False)
        db.commit()
        brand = db.get(Brand, 1)
        config = BrandConfig.model_validate(brand.config)
        assert brand.name == "LIFE, APPARENTLY." and brand.slug == "life_help"
        assert config.tagline == "Stuff you're somehow supposed to know."
        assert config.short_bio.startswith("Practical stuff for money, home, food, work")
        assert config.visual.mark == "LIFE, APPARENTLY." and config.visual.compact_mark == "LA."
        assert config.visual.background == "#F8F5EA" and config.visual.foreground == "#0F2D23"
        assert config.visual.accent == "#D4FF3E" and config.visual.muted == "#1A1A1A"
        assert config.visual.headline_font == config.visual.body_font == "Manrope"
        assert config.pillars == ["routines & weekly resets"]
        assert config.permissions["publish"] == "APPROVAL"
        assert db.scalar(select(func.count()).select_from(Knowledge).where(Knowledge.brand_id == 1)) == 1
        content = db.scalar(select(Content).where(Content.brand_id == 1))
        assert (content.id, content.revision, content.slides, content.knowledge_refs, content.generation) == content_snapshot
        assert db.get(Brand, 2).config == other_before
        templates = list(db.scalars(select(Template).where(Template.brand_id == 1).order_by(Template.id)))
        assert templates[0].config["mark"] == "LIFE, APPARENTLY."
        assert templates[0].config["background"] == "#F8F5EA"
        assert templates[1].config["accent"] == "#AABBCC"
        assert BrandConfig.model_validate(db.get(Brand, 2).config).editorial.description == "Reem voice"
    finally:
        db.close()
        engine.dispose()


def test_brand_admin_palette_edit_updates_inherited_template_only():
    engine, db = seeded_db()
    try:
        update(db)
        db.commit()
        brand = db.get(Brand, 1)
        payload = BrandInput.model_validate({"name": brand.name, "slug": brand.slug,
                                             "description": brand.description, "config": brand.config})
        payload.config.visual.accent = "#C0FF44"
        save_brand(db, payload, brand_id=1)
        db.commit()
        templates = list(db.scalars(select(Template).where(Template.brand_id == 1).order_by(Template.id)))
        assert templates[0].config["accent"] == "#C0FF44"
        assert templates[1].config["accent"] == "#AABBCC"
        assert db.get(Brand, 2).config["visual"]["accent"] == "#B5A5F5"
    finally:
        db.close()
        engine.dispose()


def test_renderer_uses_configured_public_marks_and_numbered_layouts(monkeypatch):
    engine, db = seeded_db()
    try:
        assert BrandConfig().visual.mark == BrandConfig().visual.compact_mark == ""
        update(db)
        db.commit()
        visual = BrandConfig.model_validate(db.get(Brand, 1).config).visual
        captured = []
        original = renderer.fit_text

        def spy(draw, value, *args, **kwargs):
            captured.append(value)
            return original(draw, value, *args, **kwargs)

        monkeypatch.setattr(renderer, "fit_text", spy)
        cover = renderer.render_slide(Slide(title="A useful guide", kind="cover"), visual)
        assert "LA." in captured and "LIFE, APPARENTLY." in captured
        assert "SAVEABLE GUIDE" not in captured and "PRACTICAL NOTES" not in captured
        assert Image.open(BytesIO(cover)).size == (1080, 1350)
        captured.clear()
        other_visual = BrandConfig.model_validate(db.get(Brand, 2).config).visual
        renderer.render_slide(Slide(title="A different guide", kind="cover"), other_visual)
        assert "REEMTEAM" in captured and "LA." not in captured and "LIFE, APPARENTLY." not in captured
        for kind, items in (("statement", []), ("checklist", ["Keys", "Bag"]), ("end", [])):
            slide = Slide(title="Set out your essentials", body="Put one useful thing aside tonight.",
                          kind=kind, items=items)
            third = Image.open(BytesIO(renderer.render_slide(slide, visual, 4, 8, numbered=True)))
            fourth = Image.open(BytesIO(renderer.render_slide(slide, visual, 5, 8, numbered=True)))
            badge = (84, 123, 228, 194)
            assert ImageChops.difference(third.crop(badge), fourth.crop(badge)).getbbox()
    finally:
        db.close()
        engine.dispose()


def test_render_only_keeps_content_provenance_and_brand_palette_separate(monkeypatch):
    engine, db = seeded_db()
    writes = {}

    class MemoryStorage:
        def write(self, key, data):
            writes[key] = data

    monkeypatch.setattr(creative_service, "LocalStorage", MemoryStorage)
    try:
        update(db)
        db.commit()
        item = db.scalar(select(Content).where(Content.brand_id == 1))
        before = (item.revision, item.slides, item.caption, item.knowledge_refs, item.generation)
        creative_service.render_content(db, 1, item.id)
        db.commit()
        assert (item.revision, item.slides, item.caption, item.knowledge_refs, item.generation) == before
        image = Image.open(BytesIO(writes[item.assets[0]["key"]]))
        assert image.getpixel((20, 20)) == (248, 245, 234)
        assert BrandConfig.model_validate(db.get(Brand, 2).config).visual.background == "#171D30"
    finally:
        db.close()
        engine.dispose()


def test_generation_context_uses_only_selected_brand_voice(monkeypatch):
    engine, db = seeded_db()
    try:
        update(db)
        other_knowledge = Knowledge(brand_id=2, category="routines & weekly resets",
                                    title="Prepare for tomorrow", body="Set out one thing tonight for tomorrow morning.",
                                    source="Reem editorial", verification="APPROVED", enabled=True)
        db.add(other_knowledge)
        db.commit()
        seen = []

        class FakeProvider:
            def generate(self, instruction, context, schema):
                seen.append(context["brand_voice"]["description"])
                if schema is EditorialPlan:
                    return EditorialPlan.model_validate({
                        "audience_promise": "Two things tonight that help tomorrow morning",
                        "content_goal": "Prepare two practical things", "angle": "A short evening plan",
                        "candidate_points": ["Pack tomorrow's bag", "Review tomorrow's calendar"],
                        "selected_points": [
                            {"headline": "Pack tomorrow's bag", "action": "Put the bag together tonight for tomorrow morning.",
                             "support_ids": [other_knowledge.id]},
                            {"headline": "Review tomorrow's calendar", "action": "Check the first appointment tonight for tomorrow morning.",
                             "support_ids": [other_knowledge.id]}],
                        "rejected_points": [], "desired_slide_count": 3,
                        "recommended_layout_sequence": ["cover", "numbered_action", "end"],
                        "caption_direction": "Describe one useful step and invite a save"})
                return GeneratedContent.model_validate({
                    "topic": context["request"]["topic"], "pillar": context["request"]["pillar"],
                    "format": "carousel", "hook": "Two things for tomorrow", "body": "",
                    "slides": [{"kind": "cover", "title": "Two things tonight", "body": "A simple plan for tomorrow.", "items": []},
                               {"kind": "numbered_action", "title": "Pack tomorrow's bag",
                                "body": "Put the bag together tonight for tomorrow morning.", "items": []},
                               {"kind": "end", "title": "Review tomorrow's calendar",
                                "body": "Check the first appointment tonight for tomorrow morning.", "items": []}],
                    "caption": "A short evening plan can make tomorrow's first step easier to find. Put one useful thing aside tonight, and save the list for later.",
                    "knowledge_refs": [other_knowledge.id]})

        monkeypatch.setattr(content_service, "provider", lambda _: FakeProvider())
        content_service.generate(db, 2, GenerationInput(topic="2 things tonight to make tomorrow easier",
                                 pillar="routines & weekly resets", format="carousel",
                                 knowledge_refs=[other_knowledge.id]))
        assert seen and all(value == "Reem voice" for value in seen)
    finally:
        db.rollback()
        db.close()
        engine.dispose()
