"""Baseline placeholder.

Run only after a live model is configured. It intentionally has no scripted fallback,
because baseline results must come from the same configured model with invoice + latest
customer email only.
"""
from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required; no baseline result was generated")
    raise SystemExit("Baseline runner requires a reviewed live evidence bundle; no result was generated")

