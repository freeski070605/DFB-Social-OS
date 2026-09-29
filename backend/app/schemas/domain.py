from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, ConfigDict, field_validator

Mode = Literal["AUTO", "APPROVAL", "MANUAL"]
Format = Literal["carousel", "single_graphic", "checklist", "steps", "do_dont", "comparison", "tip", "story", "reel_script", "short_video_script", "text_post"]
TemplateKind = Literal["checklist", "steps", "two_column", "do_dont", "statement", "tip", "cover", "end"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Visual(StrictModel):
    background: str = "#F4F0E8"
    foreground: str = "#183C36"
    accent: str = "#D9F075"
    muted: str = "#65726A"
    mark: str = "DFB / FIELD NOTES"

    @field_validator("background", "foreground", "accent", "muted")
    @classmethod
    def color(cls, value):
        import re
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            raise ValueError("Use a six-digit hex color")
        return value


class Posting(StrictModel):
    timezone: str = "America/New_York"
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], max_length=7)
    times: list[str] = Field(default_factory=lambda: ["09:00"], min_length=1, max_length=6)
    buffer_days: int = Field(default=14, ge=1, le=60)
    window_minutes: int = Field(default=120, ge=5, le=720)
    min_repeat_days: int = Field(default=14, ge=1, le=365)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError):
            raise ValueError("Unknown IANA timezone")
        return value

    @field_validator("weekdays")
    @classmethod
    def days_valid(cls, value):
        if not value or any(x not in range(7) for x in value):
            raise ValueError("Weekdays must be 0 (Monday) through 6")
        return sorted(set(value))

    @field_validator("times")
    @classmethod
    def times_valid(cls, value):
        from datetime import time
        for item in value:
            time.fromisoformat(item)
            if len(item) != 5:
                raise ValueError("Use HH:MM")
        return sorted(set(value))


class AIConfig(StrictModel):
    provider: Literal["local", "manual"] = "local"
    model: str = "qwen2.5:7b"
    temperature: float = Field(default=0.5, ge=0, le=1)


class InteractionRules(StrictModel):
    min_confidence: float = Field(default=0.9, ge=0.8, le=1)
    max_replies_hour: int = Field(default=10, ge=1, le=60)
    auto_categories: list[str] = Field(default_factory=lambda: ["positive", "simple_question"])
    blocked_terms: list[str] = Field(default_factory=list)


class BrandConfig(StrictModel):
    mission: str = ""
    voice: str = "Clear, practical, warm and accurate"
    tone: str = "Helpful without judgment"
    audience: str = ""
    pillars: list[str] = Field(default_factory=list, max_length=50)
    prohibited_topics: list[str] = Field(default_factory=list)
    terminology: list[str] = Field(default_factory=list)
    cta_rules: str = "Invite useful saves and shares. No engagement bait."
    visual: Visual = Field(default_factory=Visual)
    schedule: Posting = Field(default_factory=Posting)
    platforms: list[Literal["manual", "instagram", "facebook"]] = Field(default_factory=lambda: ["manual"])
    permissions: dict[str, Mode] = Field(default_factory=lambda: {"generate": "AUTO", "render": "AUTO", "schedule": "APPROVAL", "publish": "APPROVAL", "comment_reply": "AUTO", "dm_reply": "APPROVAL", "hide": "APPROVAL", "delete": "APPROVAL", "block": "MANUAL", "strategy": "APPROVAL"})
    interactions: InteractionRules = Field(default_factory=InteractionRules)
    moderation_rules: list[str] = Field(default_factory=list)
    knowledge_sources: list[str] = Field(default_factory=list)
    ai: AIConfig = Field(default_factory=AIConfig)
    strategy: dict = Field(default_factory=dict)
    strategy_locks: list[str] = Field(default_factory=list)


class BrandInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z][a-z0-9_\-]{1,79}$")
    description: str = ""
    enabled: bool = True
    config: BrandConfig = Field(default_factory=BrandConfig)


class KnowledgeInput(StrictModel):
    category: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=50000)
    source: str = Field(default="", max_length=2000)
    verification: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING"
    enabled: bool = True
    tags: list[str] = Field(default_factory=list, max_length=30)
    restrictions: str = Field(default="", max_length=2000)


class Slide(StrictModel):
    title: str = Field(min_length=1, max_length=180)
    body: str = Field(default="", max_length=1400)
    kind: TemplateKind = "statement"
    items: list[str] = Field(default_factory=list, max_length=10)


class DraftSlide(StrictModel):
    title: str = Field(default="", max_length=180)
    body: str = Field(default="", max_length=1400)
    kind: TemplateKind = "statement"
    items: list[str] = Field(default_factory=list, max_length=10)


class ContentInput(StrictModel):
    topic: str = Field(min_length=1, max_length=300)
    pillar: str = Field(min_length=1, max_length=120)
    format: Format = "carousel"
    hook: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=15000)
    slides: list[DraftSlide] = Field(default_factory=list, max_length=20)
    caption: str = Field(default="", max_length=2200)
    cta: str = Field(default="", max_length=500)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    knowledge_refs: list[int] = Field(default_factory=list, max_length=30)
    sources: list[str] = Field(default_factory=list, max_length=30)
    targets: list[Literal["manual", "instagram", "facebook"]] = Field(default_factory=lambda: ["manual"], min_length=1)
    parent_id: int | None = None


class GeneratedContent(ContentInput):
    hook: str = Field(min_length=1, max_length=500)
    caption: str = Field(min_length=1, max_length=2200)
    slides: list[Slide] = Field(default_factory=list, max_length=20)


class GenerationInput(StrictModel):
    topic: str = Field(min_length=3, max_length=300)
    pillar: str = Field(min_length=1, max_length=120)
    format: Format = "carousel"
    parent_id: int | None = None
    knowledge_refs: list[int] = Field(default_factory=list, max_length=8)


class ScheduleInput(StrictModel):
    run_at: datetime
    override_window: bool = False
    reason: str = ""


class Classification(StrictModel):
    category: Literal["positive", "simple_question", "complex_question", "criticism", "spam", "abuse", "sensitive", "business_inquiry", "collaboration", "support_request", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reply: str = Field(default="", max_length=1500)
    reason: str = Field(max_length=1000)


class InteractionInput(StrictModel):
    platform: Literal["manual", "instagram", "facebook"] = "manual"
    external_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(default="", max_length=200)
    author: str = Field(default="", max_length=200)
    kind: Literal["comment", "dm"] = "comment"
    body: str = Field(min_length=1, max_length=10000)


class AccountInput(StrictModel):
    platform: Literal["instagram", "facebook"]
    account_id: str = Field(pattern=r"^[0-9_]+$", max_length=200)
    token: str = Field(min_length=10, max_length=8000)
    enabled: bool = True


class TemplateInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    kind: TemplateKind
    config: Visual = Field(default_factory=Visual)
    enabled: bool = True
