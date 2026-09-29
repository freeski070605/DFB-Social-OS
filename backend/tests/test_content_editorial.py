from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.content import editorial
from app.core.errors import DomainError
from app.creative.renderer import render_slide
from app.schemas.domain import BrandConfig, EditorialPlan, Slide, Visual


def plan(points, ids, topic="2 things to do tonight to make tomorrow easier"):
    return EditorialPlan.model_validate({
        "audience_promise": "Two concrete tonight actions for an easier tomorrow",
        "content_goal": "Help the reader prepare for tomorrow tonight", "angle": "Remove morning decisions",
        "candidate_points": points, "selected_points": [
            {"headline": title, "action": "Do this before bed and leave the result ready for tomorrow.",
             "support_ids": [ids[index]]} for index, title in enumerate(points)],
        "rejected_points": [], "desired_slide_count": len(points) + 1,
        "recommended_layout_sequence": ["cover"] + ["numbered_action"] * len(points),
        "caption_direction": "Add framing and a natural invitation to try one action"})


def test_numbered_promise_count_duplicates_topic_fit_and_brand_evidence():
    topic = "2 things to do tonight to make tomorrow easier"
    assert editorial.headline_count(topic) == 2
    assert editorial.headline_count("A simpler evening routine") is None
    refs = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    valid = plan(["Pack tomorrow's bag", "Review tomorrow's calendar"], [1, 2])
    assert editorial.validate_plan(valid, topic, refs) == [1, 2]
    with pytest.raises(DomainError, match="exactly 2"):
        editorial.validate_plan(plan(["Pack tomorrow's bag"], [1]), topic, refs)
    with pytest.raises(DomainError, match="overlap"):
        editorial.validate_plan(plan(["Review tomorrow's calendar", "Check tomorrow's schedule"], [1, 2]), topic, refs)
    with pytest.raises(DomainError, match="topic promise"):
        editorial.validate_plan(plan(["Pack tomorrow's bag", "Use a morning launch routine"], [1, 2]), topic, refs)
    with pytest.raises(DomainError, match="outside the approved selection"):
        editorial.validate_plan(plan(["Pack tomorrow's bag", "Review tomorrow's calendar"], [1, 99]), topic, refs)


def test_explainable_quality_flags_generic_repetitive_off_topic_copy():
    slides = [{"kind": "cover", "title": "2 things to do tonight", "body": "", "items": []},
              {"kind": "steps", "title": "Review tomorrow's calendar", "body": "Check your schedule before bed so you know when to leave.", "items": []},
              {"kind": "steps", "title": "Use a morning launch routine", "body": "Make breakfast and start work in the morning.", "items": []}]
    result = editorial.evaluate("2 things to do tonight to make tomorrow easier", slides,
                                "Start your day off right. #Morning", [1], BrandConfig())
    assert result["score"] < 90 and result["needs_revision"]
    assert result["dimensions"]["topic_fit"]["issues"]
    assert result["dimensions"]["specificity"]["issues"]


def test_benefit_filler_is_removed_without_losing_a_concrete_instruction():
    assert not editorial.is_micro_item("Prevent last-minute rush.")
    assert not editorial.is_micro_item("Feel more in control.")
    assert editorial.is_micro_item("Put keys by the door")
    assert editorial.remove_filler_sentences(
        "Place your keys beside the bag tonight. Feel more in control.") == \
        "Place your keys beside the bag tonight."


def test_automated_editorial_score_does_not_claim_perfection():
    result = editorial.evaluate("A useful checklist", [], "A practical caption that is long enough to give useful context and a simple next step for the reader.",
                                [1], BrandConfig())
    assert result["score"] <= 95


def test_evaluator_flags_unnecessarily_formal_action_copy():
    slides = [{"kind": "cover", "title": "A simple checklist", "body": "", "items": []},
              {"kind": "numbered_action", "title": "Charge what you will carry",
               "body": "Plug in your phone before bed so it is ready in the morning.", "items": []}]
    result = editorial.evaluate("A simple checklist", slides,
                                "Plug in the device you need in the morning, then leave it where you can find it. Save this for your next evening reset.",
                                [1], BrandConfig())
    assert any("unnecessarily formal" in issue for issue in result["issues"])


@pytest.mark.parametrize("kind,title,body,items", [
    ("cover", "2 things to do tonight", "A small reset for tomorrow", []),
    ("numbered_action", "Pack tomorrow's bag", "Put the things you need by the door before bed.", []),
    ("checklist", "Check the essentials", "", ["Keys", "Bag", "Charger"]),
    ("statement", "Make tomorrow's first decision tonight", "Choose your first task before going to bed.", []),
    ("end", "Pick one reset for tonight", "Try the action that removes the most friction tomorrow.", []),
])
def test_renderer_layouts_keep_portrait_size_and_safe_type(kind, title, body, items):
    data = render_slide(Slide(title=title, body=body, kind=kind, items=items), Visual(mark="TEST BRAND"), 2, 8)
    image = Image.open(BytesIO(data))
    assert image.size == (1080, 1350)
    assert image.getpixel((20, 20)) in ((244, 240, 232), (24, 60, 54))


def test_renderer_rejects_excess_copy_instead_of_tiny_text():
    slide = Slide(title="Pack your bag", body="A" * 211, kind="numbered_action")
    with pytest.raises(DomainError, match="too long for phone reading"):
        render_slide(slide, Visual(), 2, 8)


def test_steps_renderer_keeps_explanation_when_list_items_exist():
    with_body = render_slide(Slide(title="Pack tomorrow's bag", body="Place your keys beside it tonight.",
                                   kind="steps", items=["Add your charger"]), Visual(), 2, 8)
    without_body = render_slide(Slide(title="Pack tomorrow's bag", body="",
                                      kind="steps", items=["Add your charger"]), Visual(), 2, 8)
    assert with_body != without_body
