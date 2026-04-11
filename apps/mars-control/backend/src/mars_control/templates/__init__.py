"""Template discovery for the Mars control plane (Story 8.2).

v1 ships a single template — ``tracker-ops-assistant`` — but the
discovery API is designed so that dropping another ``*.yaml`` +
``*.prompt.md`` pair into ``apps/mars-control/templates/`` is the
only step required to add more in v1.1. No per-template registration
code, no database migration, no deploy step.

See ``GET /templates`` in :mod:`mars_control.api.routes`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from schema.agent import AgentConfig

__all__ = [
    "DEFAULT_TEMPLATE_DIR",
    "TemplateSummary",
    "discover_templates",
    "load_template",
]

# Repo-relative default. Tests override with a tmp dir.
DEFAULT_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[4] / "templates"
)


@dataclass(frozen=True)
class TemplateSummary:
    """Compact public-facing view of a template for the dashboard.

    The frontend receives only these fields — never the raw ``env``
    or ``tools`` lists or the full system prompt — because the
    dashboard is not a YAML inspector. Wizard steps 4-6 will ask for
    the secret values by name, and the prompt is never shown.
    """

    name: str
    description: str
    runtime: str
    mcps: tuple[str, ...]
    system_prompt_preview: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "runtime": self.runtime,
            "mcps": list(self.mcps),
            "system_prompt_preview": self.system_prompt_preview,
        }


def _summarize(config: AgentConfig, prompt_path: Path) -> TemplateSummary:
    # Snip the prompt to one-line preview for card display. The
    # wizard's step 4-6 has the full doc; the list view just needs
    # a taste.
    preview = ""
    if prompt_path.is_file():
        raw = prompt_path.read_text(encoding="utf-8")
        # Skip markdown headers + blank lines to find the first real
        # sentence; cap at 200 chars to keep card layout predictable.
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                preview = stripped[:200]
                break
    return TemplateSummary(
        name=config.name,
        description=config.description,
        runtime=config.runtime,
        mcps=tuple(config.mcps),
        system_prompt_preview=preview,
    )


def load_template(yaml_path: Path) -> TemplateSummary:
    """Load a single template and return its summary.

    Raises:
        FileNotFoundError: if the YAML file does not exist.
        pydantic.ValidationError: if the YAML is not a valid
            AgentConfig.
    """
    config = AgentConfig.from_yaml_file(yaml_path)
    prompt_path = yaml_path.with_suffix(".prompt.md")
    return _summarize(config, prompt_path)


def discover_templates(
    template_dir: Path | None = None,
) -> list[TemplateSummary]:
    """Return every valid template in ``template_dir``.

    Invalid YAML files (malformed, schema violations) are skipped
    silently — v1 is read-only so a broken template is a build-time
    problem, not a runtime one. Sort by name for stable rendering.
    """
    directory = Path(template_dir) if template_dir else DEFAULT_TEMPLATE_DIR
    if not directory.is_dir():
        return []
    summaries: list[TemplateSummary] = []
    for yaml_path in sorted(directory.glob("*.yaml")):
        try:
            summaries.append(load_template(yaml_path))
        except Exception:  # noqa: BLE001
            # Don't let one broken template take down the dashboard.
            # The test suite's Story 8.1 gate catches broken templates
            # at CI time; this is a runtime safety net.
            continue
    return summaries
