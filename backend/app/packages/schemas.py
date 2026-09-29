from typing import Literal

from pydantic import Field, model_validator

from app.schemas.domain import StrictModel

DerivativeFormat = Literal["INSTAGRAM_CAROUSEL", "FACEBOOK_POST", "INSTAGRAM_REEL",
                           "TIKTOK_VIDEO", "YOUTUBE_SHORT", "YOUTUBE_LONGFORM", "THREADS_POST"]
PLATFORMS = {"INSTAGRAM_CAROUSEL": "instagram", "FACEBOOK_POST": "facebook",
             "INSTAGRAM_REEL": "instagram", "TIKTOK_VIDEO": "tiktok",
             "YOUTUBE_SHORT": "youtube", "YOUTUBE_LONGFORM": "youtube", "THREADS_POST": "threads"}
VIDEO_FORMATS = {"INSTAGRAM_REEL", "TIKTOK_VIDEO", "YOUTUBE_SHORT", "YOUTUBE_LONGFORM"}


class PackageInput(StrictModel):
    topic: str = Field(min_length=1, max_length=300)
    pillar: str = Field(min_length=1, max_length=120)
    audience_promise: str = Field(default="", max_length=1000)
    angle: str = Field(default="", max_length=1000)
    knowledge_refs: list[int] = Field(min_length=1, max_length=30)


class Scene(StrictModel):
    sequence: int = Field(ge=1)
    narration: str = ""
    on_screen_text: str = ""
    visual_type: Literal["original_footage", "demonstration", "graphic", "b_roll", "screen_recording"]
    visual_direction: str = ""
    estimated_seconds: int = Field(ge=1)
    knowledge_refs: list[int] = Field(default_factory=list)


class VideoPlan(StrictModel):
    title: str = Field(min_length=1)
    alternate_titles: list[str] = Field(default_factory=list)
    hook: str = Field(min_length=1)
    intro: str = ""
    target_duration_seconds: int = Field(ge=1)
    narration: str = Field(min_length=1)
    scenes: list[Scene] = Field(min_length=2)
    sections: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    b_roll: list[str] = Field(default_factory=list)
    graphics: list[str] = Field(default_factory=list)
    thumbnail_concept: str = ""
    conclusion: str = ""
    cta: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    chapter_candidates: list[str] = Field(default_factory=list)
    knowledge_refs: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def scene_order(self):
        if [scene.sequence for scene in self.scenes] != list(range(1, len(self.scenes) + 1)):
            raise ValueError("Scenes must be numbered in order")
        return self


class TextPlan(StrictModel):
    hook: str = Field(min_length=1)
    body: str = Field(min_length=1)
    cta: str = ""
    visual_direction: str = ""
    hashtags: list[str] = Field(default_factory=list)
    knowledge_refs: list[int] = Field(min_length=1)


class GenerateInput(StrictModel):
    format: DerivativeFormat
