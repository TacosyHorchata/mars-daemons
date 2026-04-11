"""Template discovery for the Mars control plane (Story 8.2).

v1 ships a single template — ``tracker-ops-assistant`` — but the
discovery API is designed so that dropping another ``*.yaml`` +
``*.prompt.md`` pair into ``apps/mars-control/templates/`` is the
only step required to add more in v1.1. No per-template registration
code, no database migration, no deploy step.

v1 deployment model: **run from a repo checkout.** ``DEFAULT_TEMPLATE_DIR``
walks up ``parents[4]`` relative to this file, which assumes the
package lives under ``apps/mars-control/backend/src/mars_control/``.
If Mars is ever packaged as a wheel or installed via pip, this
default breaks and the admin must set ``MARS_TEMPLATE_DIR`` explicitly
or pass ``template_dir`` to ``create_control_app``. We crash at route
time with 500 when that directory does not exist so the failure is
loud — previously this returned ``{"templates": []}`` which hid the
misconfiguration behind an empty-state UI.

See ``GET /templates`` in :mod:`mars_control.api.routes`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from schema.agent import AgentConfig

__all__ = [
    "DEFAULT_TEMPLATE_DIR",
    "MissingPromptError",
    "TemplateDirMissingError",
    "TemplateSummary",
    "discover_templates",
    "load_template",
]

_log = logging.getLogger(__name__)

# Repo-relative default — v1 is run-from-checkout only, see module
# docstring for the wheel/pip escape hatch.
DEFAULT_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[4] / "templates"
)


class TemplateDirMissingError(FileNotFoundError):
    """Raised when the configured template directory does not exist.

    v1 prefers a loud failure here over silently returning an empty
    list — an empty list looks like "no templates yet" in the UI,
    which hides a misconfigured ``MARS_TEMPLATE_DIR`` indefinitely.
    """


class MissingPromptError(FileNotFoundError):
    """Raised when a template YAML has no sibling ``*.prompt.md``.

    The template is advertised as a deployable unit in
    ``GET /templates``, so the control plane must refuse to surface
    a template that cannot actually be deployed because its system
    prompt is missing. Story 8.4's deploy endpoint would hit the
    same error at spawn time — we want to catch it at list time so
    the failure is visible on the dashboard, not after the wizard
    spends 3 minutes collecting OAuth + secrets.
    """


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
    # a taste. Caller guarantees prompt_path exists (load_template
    # validates before calling) so the is_file check here is a
    # defensive assertion, not a fallback.
    if not prompt_path.is_file():
        raise MissingPromptError(
            f"sibling prompt file missing for {config.name!r}: "
            f"expected {prompt_path}"
        )
    raw = prompt_path.read_text(encoding="utf-8")
    preview = ""
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
        yaml.YAMLError: if the YAML is malformed.
        pydantic.ValidationError: if the YAML is not a valid
            :class:`AgentConfig`.
        MissingPromptError: if the sibling ``*.prompt.md`` file
            does not exist on disk. The template is advertised but
            cannot be deployed without the prompt, so we surface
            this at list time.
    """
    config = AgentConfig.from_yaml_file(yaml_path)
    prompt_path = yaml_path.with_suffix(".prompt.md")
    return _summarize(config, prompt_path)


def discover_templates(
    template_dir: Path | None = None,
) -> list[TemplateSummary]:
    """Return every valid template in ``template_dir``.

    Broken templates are logged at WARNING and skipped — one bad
    YAML cannot take down the dashboard — but only the specific
    expected exception types (YAML parse errors, schema violations,
    missing prompt files, value errors from the schema validators).
    Unexpected errors (programming bugs, filesystem permission
    issues, decoding faults) propagate so they surface as 500s
    with tracebacks instead of silently disappearing templates.

    Raises:
        TemplateDirMissingError: if ``template_dir`` resolves to a
            non-existent directory. v1 prefers a loud failure here
            over returning an empty list that hides a misconfigured
            ``MARS_TEMPLATE_DIR``.
    """
    directory = Path(template_dir) if template_dir else DEFAULT_TEMPLATE_DIR
    if not directory.is_dir():
        raise TemplateDirMissingError(
            f"template directory does not exist: {directory} "
            f"(set MARS_TEMPLATE_DIR or pass template_dir to "
            f"create_control_app)"
        )
    summaries: list[TemplateSummary] = []
    for yaml_path in sorted(directory.glob("*.yaml")):
        try:
            summaries.append(load_template(yaml_path))
        except (
            yaml.YAMLError,
            ValidationError,
            ValueError,
            MissingPromptError,
        ) as exc:
            _log.warning(
                "skipping broken template %s: %s",
                yaml_path,
                exc,
            )
            continue
    return summaries
