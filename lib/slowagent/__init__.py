# slowagent — LLM-based monitoring helpers for SlowDash
# Author: Yao Yin

from .secrets import get_secret, SecretError
from .webcam import WebcamSource, open_webcam
from .llm import ClaudeVisionExtractor, ExtractionResult, LLMError

__all__ = [
    "get_secret", "SecretError",
    "WebcamSource", "open_webcam",
    "ClaudeVisionExtractor", "ExtractionResult", "LLMError",
]
