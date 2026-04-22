"""use_skill — activate/deactivate skills via SkillsProvider."""

from __future__ import annotations

from ..core.config import get_config
from ..core.providers import get_skills_provider
from ..core.tools import AuthContext, BaseTool, ToolResult, get_tool_by_name


class UseSkillTool(BaseTool):
    name = "use_skill"
    description = (
        "Activate or deactivate a skill (instruction set). "
        "Available skills are listed in the system prompt."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the skill.",
            },
            "action": {
                "type": "string",
                "enum": ["activate", "deactivate"],
                "description": "Whether to activate or deactivate the skill.",
                "default": "activate",
            },
            "args": {
                "type": "object",
                "description": "Optional arguments for the skill template.",
                "default": {},
            },
        },
        "required": ["name"],
    }

    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        skill_name = input.get("name", "")
        action = input.get("action", "activate")
        args = input.get("args", {})

        if action == "deactivate":
            return self._deactivate(skill_name, state)

        return await self._activate(skill_name, args, auth, state)

    async def _activate(self, skill_name: str, args: dict, auth: AuthContext, state: dict) -> ToolResult:
        provider = get_skills_provider()
        if provider is None:
            return ToolResult(success=False, error="Skills not configured.")

        skill = await provider.get_skill(skill_name, auth.org_id)
        if not skill:
            try:
                available = await provider.list_skills(auth.org_id)
                names = ", ".join(s.name for s in available)
            except Exception:
                names = "(unavailable)"
            return ToolResult(
                success=False,
                error=f"Skill '{skill_name}' not found. Available: {names}",
            )

        missing = [t for t in skill.required_tools if not get_tool_by_name(t)]
        if missing:
            return ToolResult(
                success=False,
                error=f"Skill '{skill_name}' requires tools not available: {missing}",
            )

        active_skills = state.get("active_skills", [])
        if any(s["name"] == skill_name for s in active_skills):
            return ToolResult(
                success=True,
                data={"status": "already_active", "skill": skill_name},
            )

        config = get_config()
        if len(active_skills) >= config.max_active_skills:
            return ToolResult(
                success=False,
                error=f"Maximum {config.max_active_skills} skills can be active at once. Deactivate one first.",
            )

        active_skills.append({
            "name": skill.name,
            "activation_mode": skill.activation_mode,
            "prompt": skill.prompt_template,
            "args": args,
        })
        state["active_skills"] = active_skills

        return ToolResult(
            success=True,
            data={
                "status": "activated",
                "skill": skill.name,
                "mode": skill.activation_mode,
            },
        )

    def _deactivate(self, skill_name: str, state: dict) -> ToolResult:
        active_skills = state.get("active_skills", [])
        before_count = len(active_skills)
        state["active_skills"] = [s for s in active_skills if s["name"] != skill_name]

        if len(state["active_skills"]) == before_count:
            return ToolResult(success=False, error=f"Skill '{skill_name}' is not active")

        return ToolResult(success=True, data={"status": "deactivated", "skill": skill_name})
