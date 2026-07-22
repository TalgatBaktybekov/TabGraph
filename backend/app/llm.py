"""LLM helpers for extraction and QA."""

import os
from pathlib import Path

from google import genai
from google.genai import types

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_client: genai.Client | None = None


def load_env() -> None:
    """Load from .env"""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def extraction_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def qa_model() -> str:
    return os.environ.get("GEMINI_QA_MODEL", "gemini-2.5-flash")


def discovery_model() -> str:
    """Cheap model for the discovery-job LLM judge and community summaries."""
    return os.environ.get("GEMINI_DISCOVERY_MODEL", "gemini-2.5-flash-lite")


def get_client() -> genai.Client:
    global _client
    if _client is None:
        load_env()
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set (.env)")
        _client = genai.Client(api_key=api_key)
    return _client


def generate_json(model: str, system: str, prompt: str) -> str:
    """Generate JSON-shaped text with Gemini."""
    response = get_client().models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.1,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return response.text or ""


def generate_text(model: str, system: str, prompt: str) -> str:
    """Generate free-form text with Gemini."""
    response = get_client().models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
        ),
    )
    return response.text or ""
