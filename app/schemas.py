from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.config import MAX_SHARE_TEXT_LENGTH


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    share_text: str = Field(min_length=1, max_length=MAX_SHARE_TEXT_LENGTH)
