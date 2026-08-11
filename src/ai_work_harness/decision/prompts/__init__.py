"""Versioned, importable prompts used by the OpenAI provider."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .evaluation_v1 import EVALUATION_V1
from .recommendation_v1 import RECOMMENDATION_V1


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    prompt_id: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


EVALUATION_PROMPT = PromptTemplate("evaluation-v1", EVALUATION_V1)
RECOMMENDATION_PROMPT = PromptTemplate("recommendation-v1", RECOMMENDATION_V1)

__all__ = ["EVALUATION_PROMPT", "RECOMMENDATION_PROMPT", "PromptTemplate"]
