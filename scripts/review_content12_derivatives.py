"""Editorial correction of two controlled local model drafts; preserves the raw drafts."""
from app.audit.service import record
from app.db.session import SessionLocal
from app.models import ContentDerivative
from app.packages.schemas import VideoPlan
from app.packages.service import evaluate_output


def scene(sequence, narration, text, visual_type, visual_direction, seconds, refs):
    return {"sequence": sequence, "narration": narration, "on_screen_text": text,
            "visual_type": visual_type, "visual_direction": visual_direction,
            "estimated_seconds": seconds, "knowledge_refs": refs}


SHORT_SCENES = [
    scene(1, "Tomorrow has a few decisions you can make tonight. Start with a two-minute look at your calendar and priorities.",
          "2-minute look ahead", "screen_recording", "Film a real calendar with private details obscured; point to the first appointment and one priority.", 12, [224]),
    scene(2, "Then do one small job for future-you. Put the thing you will need first by the door, or pack what you can now.",
          "Do one small job", "demonstration", "Hands place a bag and keys by the door; show an alternate lunch container without implying everyone needs both.", 12, [216]),
    scene(3, "Give your space a short closing reset. Put away what you are done with and leave tomorrow's starting point easy to find.",
          "Close the day", "original_footage", "Film a real work surface before and after a brief reset, with the next day's item visible.", 12, [204]),
    scene(4, "That is enough. A quick look ahead, one useful task, and a place to start in the morning.",
          "Pick one thing tonight", "graphic", "Simple three-part recap over original footage, then fade to LIFE, APPARENTLY. wordmark.", 9, [204, 216, 224]),
]
SHORT = {
    "title": "Make tomorrow easier tonight", "alternate_titles": ["A small reset for tomorrow"],
    "hook": "Tomorrow has a few decisions you can make tonight.", "intro": "",
    "target_duration_seconds": 45,
    "narration": " ".join(s["narration"] for s in SHORT_SCENES), "scenes": SHORT_SCENES,
    "sections": [], "examples": ["Put tomorrow's first item by the door."],
    "b_roll": ["Real calendar and bag close-ups"], "graphics": ["Three-part recap"],
    "thumbnail_concept": "A simple evening desk reset with one item ready for morning.",
    "conclusion": "A quick look ahead, one useful task, and a place to start.",
    "cta": "Choose one small thing to set up tonight.",
    "description": "A short, practical evening reset: review tomorrow, prepare one thing, and close out your space.",
    "hashtags": [], "chapter_candidates": [], "knowledge_refs": [204, 216, 224],
}

LONG_SCENES = [
    scene(1, "Tomorrow does not need a perfect evening routine. In this guide, we will make a small, repeatable closing routine from three parts: look ahead, prepare one thing, and reset the space you will use first. I will show what each part looks like, plus how to shorten it when you have almost no time.",
          "A closing routine that fits", "original_footage", "Open on an ordinary evening at home; show a calendar, a bag, and a lived-in counter, not stock-photo perfection.", 45, [204, 216, 224]),
    scene(2, "First, look at tomorrow. Take two minutes to check the calendar and your priorities. What time do you need to leave or start? Is there one appointment, task, or item that changes what you should prepare? Write down only the first useful priority. The point is to notice what tomorrow asks of you before you stop for the night.",
          "Check calendar + first priority", "screen_recording", "Demonstrate a sample calendar with fictional entries and a short paper note; obscure real personal data.", 60, [224]),
    scene(3, "Now choose one small job for future-you. It might be placing your keys with your bag, filling a water bottle, or putting a lunch container where you will see it. You do not need to do all three. Pick the one that matches the calendar you just checked. That makes the task specific, and it gives you a visible starting point in the morning.",
          "Prepare one useful thing", "demonstration", "Show three optional objects, then choose only one based on a fictional early appointment.", 60, [216, 224]),
    scene(4, "Next, give the home or workspace a short closing reset. Put away what you have finished using. Leave the first surface or item you need tomorrow easy to find. A closing routine can be small: a cleared corner, a bag in one place, and a note beside it. This is practical guidance, not a rule that every room needs to be spotless.",
          "A short closing reset", "original_footage", "Film a brief, realistic reset of a desk and entryway; avoid a sped-up whole-house cleaning montage.", 60, [204]),
    scene(5, "Here is how the three parts work together. Imagine an early appointment tomorrow. The calendar check tells you when you need to leave. Your one small task is putting the appointment paperwork in your bag. The closing reset is leaving that bag where you will pick it up. If tomorrow is a work-from-home day instead, the same approach might end with a clear desk and your first task written down. The details change; the three-part structure stays useful.",
          "Two real-life examples", "demonstration", "Show two fictional scenarios side by side: early appointment and work-from-home morning, each with a distinct physical setup.", 75, [204, 216, 224]),
    scene(6, "If tonight is already full, make the routine smaller. Check one calendar item and prepare one object. If you have more time, add the closing reset. Try it once and notice which part actually helps your morning. Keep what fits your life and skip what does not. A useful evening routine is a practical tool, not a test you can fail.",
          "Start with one part", "graphic", "Use a simple decision graphic over footage: two minutes available versus a little more time; end on the real setup, not animated slides alone.", 60, [204, 216, 224]),
]
LONG = {
    "title": "A practical evening reset for an easier tomorrow",
    "alternate_titles": ["What to do tonight when tomorrow already looks busy", "Build a small closing routine that fits your life"],
    "hook": "Tomorrow does not need a perfect evening routine.",
    "intro": "Build a small closing routine from a two-minute look ahead, one useful task, and a short space reset.",
    "target_duration_seconds": 360,
    "narration": " ".join(s["narration"] for s in LONG_SCENES), "scenes": LONG_SCENES,
    "sections": ["The three-part closing routine", "Look ahead for two minutes", "Prepare one useful thing",
                 "Reset the starting space", "Two example mornings", "Make the routine smaller when needed"],
    "examples": ["Early appointment: check the time, put paperwork in the bag, leave it by the door.",
                 "Work-from-home morning: review the first task, set out what is needed, clear the desk corner."],
    "b_roll": ["Real calendar with fictional entries", "Hands placing an item in a bag", "Short desk reset"],
    "graphics": ["Three-part overview", "Two-minute versus longer version of the routine"],
    "thumbnail_concept": "An ordinary desk at night with a calendar, one packed item, and a clear starting spot; short title overlay.",
    "conclusion": "Keep the parts that make tomorrow's starting point easier to see.",
    "cta": "Try one part tonight and keep what fits your life.",
    "description": "A grounded guide to a small evening reset: review tomorrow, prepare one useful thing, and leave a clear starting point. Includes two examples and a shorter version for busy nights.",
    "hashtags": [], "chapter_candidates": ["00:00 The three parts", "00:45 Look ahead", "01:45 Prepare one thing",
        "02:45 Reset the space", "03:45 Two example mornings", "05:00 Make it smaller"],
    "knowledge_refs": [204, 216, 224],
}


def apply_review(db, derivative_id, payload):
    item = db.get(ContentDerivative, derivative_id)
    assert item and item.brand_id == 1 and item.package_id == 1
    if item.generation.get("editorial_correction"):
        return item
    plan = VideoPlan.model_validate(payload)
    assessment = evaluate_output(plan, item.format, {204, 216, 224})
    if assessment["issues"]:
        raise ValueError(f"Editorial correction did not pass: {assessment['issues']}")
    original = dict(item.output)
    item.output = plan.model_dump()
    item.used_knowledge_refs = plan.knowledge_refs
    item.evaluation = assessment
    item.generation = {**item.generation, "editorial_correction": "human_reviewed",
                       "model_draft": original}
    record(db, "derivative.editorial_correction", item.id, 1, "controlled_local_test",
           after={"format": item.format, "knowledge_refs": plan.knowledge_refs})
    return item


if __name__ == "__main__":
    with SessionLocal() as db:
        for key, payload in ((2, SHORT), (3, LONG)):
            item = apply_review(db, key, payload)
            print(item.format, item.id, item.evaluation)
        db.commit()
