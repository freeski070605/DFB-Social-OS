"""Small, inspectable editorial checks shared by generation and review."""

import re
from difflib import SequenceMatcher
from app.core.errors import DomainError

STOP = {"the", "and", "for", "you", "your", "tomorrow", "tonight", "things", "thing", "make", "with", "that", "this", "from", "before", "after", "into", "what", "when", "one", "more", "up", "off"}
ALIASES = {"calendar": "schedule", "agenda": "schedule", "appointments": "schedule",
           "clothes": "outfit", "clothing": "outfit", "laundry": "outfit",
           "pack": "prepare", "packing": "prepare", "preparing": "prepare", "prep": "prepare",
           "check": "review", "look": "review", "plan": "review"}
GENERIC = ("start your day off right", "level up", "transform your", "unlock", "game changer",
           "you've got this", "take control of", "ready to", "here are some tips",
           "in today's fast-paced world", "elevate your", "boost productivity", "streamline",
           "reduce mental load", "boost self-awareness", "stay organized", "end the day positively")
BENEFIT_STARTS = ("save time", "reduce stress", "reduce last-minute", "boost", "improve", "streamline",
                  "ensure", "stay organized", "ease into", "feel better", "create a calm",
                  "prevent last-minute", "avoid last-minute", "be prepared", "feel more",
                  "stay connected", "peace of mind", "prioritize your day")
CLAIMS = ("sleep quality", "productivity", "stress", "self-awareness", "mental load")


def headline_count(topic):
    match = re.match(r"^\s*(\d{1,2})\s+(?:things?|ways?|steps?|ideas?|actions?|tips?|checks?)\b", topic.casefold())
    return int(match.group(1)) if match and 2 <= int(match.group(1)) <= 15 else None


def tokens(value):
    return {ALIASES.get(word, word) for word in re.findall(r"[a-z]+", value.casefold()) if word not in STOP}


def near_duplicate(first, second):
    a, b = tokens(first), tokens(second)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= .55 or SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio() >= .78


def is_micro_item(value):
    return bool(value.strip()) and not value.strip().casefold().startswith(BENEFIT_STARTS)


def remove_filler_sentences(body):
    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    kept = [sentence for sentence in sentences if not sentence.casefold().startswith(BENEFIT_STARTS)]
    return " ".join(kept) if kept else body.strip()


def topic_mismatch(topic, headline, action):
    topic_words = tokens(topic)
    text = (headline + " " + action).casefold()
    if "tonight" in topic.casefold():
        title = headline.casefold()
        if ("morning routine" in title or "morning launch" in title or "start your morning" in title) and not any(term in title for term in ("prepare", "plan", "set up", "tonight")):
            return True
        if ("weekly reset" in title or "next seven days" in title) and not any(term in title for term in ("tonight", "before bed")):
            return True
        if "monday" in title and "monday" not in topic.casefold() and "tonight" not in title:
            return True
        return False
    return bool(topic_words and not (topic_words & tokens(text)) and not any(
        word in text for word in ("tonight", "tomorrow", "night", "morning", "prepare", "schedule")))


def unsupported_claim(body, evidence):
    return next((claim for claim in CLAIMS if claim in body.casefold() and not any(
        claim in source.casefold() for source in evidence)), None)


def missing_temporal_link(topic, title, body):
    return ("tonight" in topic.casefold() and "tomorrow" in topic.casefold() and not any(
        term in (title + " " + body).casefold() for term in
        ("tonight", "before bed", "this evening", "tomorrow", "morning", "next day", "overnight")))


def validate_plan(plan, topic, refs):
    expected = headline_count(topic)
    points = plan.selected_points
    if expected and len(points) != expected:
        raise DomainError(f"Editorial plan needs exactly {expected} distinct actions")
    # The action list is authoritative. Models often count the cover as an action or omit it
    # from a layout list; normalizing those advisory fields never changes the promised actions.
    plan.desired_slide_count = len(points) + 1
    layouts = list(plan.recommended_layout_sequence)
    if not layouts or layouts[0] != "cover":
        layouts.insert(0, "cover")
    plan.recommended_layout_sequence = (layouts + ["numbered_action"] * len(points))[:len(points) + 1]
    eligible = {row.id for row in refs}
    for index, point in enumerate(points):
        if not set(point.support_ids) <= eligible:
            raise DomainError(f"Editorial point {index + 1} cites knowledge outside the approved selection")
        if topic_mismatch(topic, point.headline, point.action):
            raise DomainError(f"Editorial point {index + 1} does not fit the topic promise")
        if len(point.headline) > 75:
            raise DomainError(f"Editorial point {index + 1} exceeds the phone-readable copy budget")
        if len(point.action) > 200:
            raise DomainError(f"Editorial point {index + 1} explanation exceeds the phone-readable copy budget")
        condensed = []
        for value in point.items:
            cleaned = " ".join(value.split())
            if len(cleaned) > 65:
                cleaned = re.split(r"[.;:]", cleaned, maxsplit=1)[0].strip()
            if cleaned and len(cleaned) <= 65 and is_micro_item(cleaned):
                condensed.append(cleaned)
        point.items = condensed[:4]
        if any(near_duplicate(point.headline, previous.headline) or
               (len(tokens(point.headline) & tokens(previous.headline)) >= 2 and
                near_duplicate(point.action, previous.action)) for previous in points[:index]):
            raise DomainError(f"Editorial points {index + 1} and an earlier point overlap")
    return sorted({key for point in points for key in point.support_ids})


def evaluate(topic, slides, caption, knowledge_refs, config, plan=None, evidence=None):
    """Heuristic editorial signals; the score is a review aid, not a proof of quality."""
    dimensions = {name: {"score": maximum, "max": maximum, "issues": []} for name, maximum in (
        ("topic_fit", 20), ("distinctness", 20), ("specificity", 20), ("actionability", 15),
        ("grounding", 10), ("readability", 10), ("structure", 5))}
    blocking = []

    def deduct(name, amount, issue, hard=False):
        part = dimensions[name]
        part["score"] = max(0, part["score"] - amount)
        part["issues"].append(issue)
        if hard:
            blocking.append(issue)

    expected = headline_count(topic)
    actions = slides[1:] if slides and slides[0].get("kind") == "cover" else slides
    if expected and len(actions) != expected:
        deduct("structure", 5, f"Headline promises {expected} actions but there are {len(actions)}", True)
    if slides and slides[0].get("kind") != "cover":
        deduct("structure", 3, "First slide should be a cover")
    if len(actions) >= 5 and len({slide.get("kind") for slide in actions}) == 1:
        deduct("structure", 3, "Slides need more visual rhythm than one repeated layout")
    if not knowledge_refs:
        deduct("grounding", 10, "No approved knowledge attached")
    banned = tuple(phrase.casefold() for phrase in config.editorial.banned_phrases) + GENERIC
    if any(phrase in caption.casefold() for phrase in banned) or re.search(r"#\w+", caption):
        deduct("specificity", 5, "Caption uses generic phrasing or hashtags in prose", True)
    if len(caption.strip()) < 90:
        deduct("specificity", 3, "Caption needs useful framing beyond a one-line summary", True)
    action_start = 2 if len(actions) < len(slides) else 1
    for position, slide in enumerate(actions):
        index = position + action_start
        title, body = slide.get("title", ""), slide.get("body", "")
        if topic_mismatch(topic, title, body):
            deduct("topic_fit", 7, f"Slide {index} is loosely tied to the topic promise", True)
        if missing_temporal_link(topic, title, body):
            deduct("topic_fit", 3, f"Slide {index} could state its tonight-to-tomorrow link more clearly")
        if len(tokens(title)) < 2 or len(body.strip()) < 35:
            deduct("specificity", 3, f"Slide {index} needs a more specific action or explanation")
        if any(phrase in body.casefold() for phrase in GENERIC) or any(
                phrase in body.casefold() for phrase in BENEFIT_STARTS):
            deduct("specificity", 3, f"Slide {index} uses generic benefit language instead of useful detail")
        if evidence is not None and (claim := unsupported_claim(body, evidence)):
            deduct("grounding", 5, f"Slide {index} makes an unsupported {claim} claim", True)
        if not re.match(r"^(put|set|write|pack|lay|check|review|choose|clear|prep|prepare|charge|move|make|fill|pick|leave|place|list|reset|decide|schedule|open|close|take|gather|sort|create|use|do|keep|finish)\b", title.casefold()):
            deduct("actionability", 2, f"Slide {index} could state the action more directly")
        if len(title) > 75 or len(body) > 200 or any(len(value) > 65 for value in slide.get("items", [])):
            deduct("readability", 3, f"Slide {index} is too dense for phone reading")
        if any(near_duplicate(title, earlier.get("title", "")) or
               near_duplicate(title + " " + body, earlier.get("title", "") + " " + earlier.get("body", ""))
               for earlier in actions[:position]):
            deduct("distinctness", 7, f"Slide {index} overlaps an earlier action", True)
    issues = [issue for part in dimensions.values() for issue in part["issues"]]
    # Automated checks cannot establish that a post is publication-perfect.
    score = min(95, sum(part["score"] for part in dimensions.values()))
    return {"score": score, "dimensions": dimensions, "issues": issues, "blocking_issues": blocking,
            "needs_revision": bool(issues),
            "note": "Editorial signals for human review, capped at 95; human approval is still required"}
