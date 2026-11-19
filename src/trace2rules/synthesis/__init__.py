"""Optional LLM wrapper for report prompts."""

from .models import Rule, Synthesis
from .service import synthesize_report

__all__ = ["Rule", "Synthesis", "synthesize_report"]
