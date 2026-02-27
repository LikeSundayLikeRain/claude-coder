"""Skill discovery, parsing, and prompt resolution.

This module implements the Agent Skills standard:
- Project skills: .claude/skills/<name>/SKILL.md
- Personal skills: ~/.claude/skills/<name>/SKILL.md
- Plugin skills: ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/
- Legacy commands: .claude/commands/<name>.md and ~/.claude/commands/<name>.md

Skills use YAML frontmatter for metadata and support argument placeholders
in the body content.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog
import yaml  # type: ignore[import-untyped]

logger = structlog.get_logger(__name__)

# Default paths
DEFAULT_PERSONAL_SKILLS_DIR = Path.home() / ".claude" / "skills"
DEFAULT_PERSONAL_COMMANDS_DIR = Path.home() / ".claude" / "commands"
DEFAULT_PLUGINS_JSON = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
DEFAULT_SETTINGS_JSON = Path.home() / ".claude" / "settings.json"


@dataclass(frozen=True)
class SkillMetadata:
    """Metadata for a discovered skill."""

    name: str
    description: str
    argument_hint: Optional[str]
    user_invocable: bool
    allowed_tools: list[str] = field(default_factory=list)
    # 'project' | 'personal' | 'plugin' | 'legacy_project' | 'legacy_personal'
    source: str = "project"
    file_path: Path = field(default_factory=lambda: Path())


def _parse_frontmatter(content: str) -> tuple[Optional[dict], str]:
    """Parse YAML frontmatter from content.

    Args:
        content: File content potentially containing frontmatter

    Returns:
        Tuple of (frontmatter_dict, body_content)
        If no frontmatter, returns (None, original_content)
    """
    # Check for frontmatter markers
    if not content.startswith("---"):
        return None, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content

    try:
        frontmatter = yaml.safe_load(parts[1])
        body = parts[2].lstrip("\n")
        return frontmatter, body
    except yaml.YAMLError as e:
        logger.warning("Failed to parse YAML frontmatter", error=str(e))
        return None, content


def _scan_skills_dir(
    skills_dir: Path,
    source: str,
    *,
    recursive: bool = True,
    name_prefix: str = "",
) -> list[SkillMetadata]:
    """Scan a skills directory for SKILL.md files.

    Args:
        skills_dir: Directory containing skill subdirectories
        source: Source identifier for discovered skills
        recursive: If True, recursively scan (monorepo support).
            If False, only scan direct child directories.
        name_prefix: Optional prefix for skill names (e.g., "plugin-name:")

    Returns:
        List of discovered skill metadata
    """
    skills: list[SkillMetadata] = []

    if not skills_dir.exists():
        return skills

    if recursive:
        skill_files = skills_dir.rglob("SKILL.md")
    else:
        # Only check direct child directories: skills_dir/<name>/SKILL.md
        skill_files = skills_dir.glob("*/SKILL.md")

    for skill_file in skill_files:
        skill_path = skill_file.parent
        if not skill_file.is_file():
            continue

        try:
            content = skill_file.read_text()
            frontmatter, _ = _parse_frontmatter(content)

            if frontmatter is None:
                logger.warning(
                    "Skill missing frontmatter",
                    path=str(skill_file),
                )
                continue

            # Skip non-invocable skills (default is True per Agent Skills spec)
            if not frontmatter.get("user-invocable", True):
                continue

            raw_name = frontmatter.get("name", skill_path.name)
            skill_name = f"{name_prefix}{raw_name}" if name_prefix else raw_name

            skills.append(
                SkillMetadata(
                    name=skill_name,
                    description=frontmatter.get("description", ""),
                    argument_hint=frontmatter.get("argument-hint"),
                    user_invocable=True,
                    allowed_tools=frontmatter.get("allowed-tools", []),
                    source=source,
                    file_path=skill_file,
                )
            )
        except Exception as e:
            logger.warning(
                "Failed to parse skill",
                path=str(skill_file),
                error=str(e),
            )

    return skills


def _scan_legacy_commands_dir(
    commands_dir: Path, source: str
) -> list[SkillMetadata]:
    """Scan a legacy commands directory for .md files.

    Args:
        commands_dir: Directory containing command .md files
        source: Source identifier for discovered commands

    Returns:
        List of discovered skill metadata
    """
    skills: list[SkillMetadata] = []

    if not commands_dir.exists():
        return skills

    for command_file in commands_dir.glob("*.md"):
        try:
            skills.append(
                SkillMetadata(
                    name=command_file.stem,
                    description="",
                    argument_hint=None,
                    user_invocable=True,
                    allowed_tools=[],
                    source=source,
                    file_path=command_file,
                )
            )
        except Exception as e:
            logger.warning(
                "Failed to read legacy command",
                path=str(command_file),
                error=str(e),
            )

    return skills


def _read_enabled_plugins(
    settings_json: Path = DEFAULT_SETTINGS_JSON,
) -> Optional[dict[str, bool]]:
    """Read enabledPlugins map from Claude Code settings.json.

    Args:
        settings_json: Path to ~/.claude/settings.json

    Returns:
        Dict mapping plugin keys to enabled status, or None if unavailable
    """
    if not settings_json.exists():
        return None

    try:
        data = json.loads(settings_json.read_text())
        return data.get("enabledPlugins")
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _get_plugin_skill_dirs(
    plugins_json: Path = DEFAULT_PLUGINS_JSON,
    settings_json: Path = DEFAULT_SETTINGS_JSON,
) -> list[tuple[Path, str]]:
    """Read installed_plugins.json and return skills directories for active plugins.

    Only includes plugins that are enabled in settings.json (enabledPlugins).
    If enabledPlugins is not present, all installed plugins are included.

    The plugin key format is "plugin-name@marketplace". The part before '@'
    is the plugin name used as a namespace prefix for skill names.

    Args:
        plugins_json: Path to installed_plugins.json
        settings_json: Path to ~/.claude/settings.json

    Returns:
        List of (skills_dir_path, plugin_name) tuples
    """
    if not plugins_json.exists():
        return []

    enabled_map = _read_enabled_plugins(settings_json)

    try:
        data = json.loads(plugins_json.read_text())
        plugins = data.get("plugins", {})
        result: list[tuple[Path, str]] = []

        for plugin_key, installations in plugins.items():
            if not isinstance(installations, list):
                continue

            # Skip disabled plugins
            if enabled_map is not None and not enabled_map.get(plugin_key, True):
                logger.debug(
                    "Skipping disabled plugin",
                    plugin=plugin_key,
                )
                continue

            # Extract plugin name from key: "superpowers@marketplace" -> "superpowers"
            plugin_name = plugin_key.split("@")[0] if "@" in plugin_key else plugin_key

            for install in installations:
                install_path = install.get("installPath")
                if install_path:
                    skills_dir = Path(install_path) / "skills"
                    if skills_dir.is_dir():
                        result.append((skills_dir, plugin_name))

        return result

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(
            "Failed to read installed plugins",
            path=str(plugins_json),
            error=str(e),
        )
        return []


def discover_skills(
    project_dir: Path,
    personal_skills_dir: Optional[Path] = None,
    personal_commands_dir: Optional[Path] = None,
    plugins_json: Optional[Path] = None,
    settings_json: Optional[Path] = None,
) -> list[SkillMetadata]:
    """Discover skills from filesystem.

    Scans in priority order:
    1. Project skills: <project_dir>/.claude/skills/
    2. Personal skills: personal_skills_dir or ~/.claude/skills/
    3. Plugin skills: from installed_plugins.json (active plugins only)
    4. Legacy project commands: <project_dir>/.claude/commands/
    5. Legacy personal commands: personal_commands_dir or ~/.claude/commands/

    Deduplicates by name, with earlier discoveries taking precedence.

    Args:
        project_dir: Project root directory
        personal_skills_dir: Personal skills directory
            (default: ~/.claude/skills)
        personal_commands_dir: Personal commands directory
            (default: ~/.claude/commands)
        plugins_json: Path to installed_plugins.json
            (default: ~/.claude/plugins/installed_plugins.json)
        settings_json: Path to ~/.claude/settings.json for enabledPlugins
            (default: ~/.claude/settings.json)

    Returns:
        List of discovered skill metadata
    """
    if personal_skills_dir is None:
        personal_skills_dir = DEFAULT_PERSONAL_SKILLS_DIR
    if personal_commands_dir is None:
        personal_commands_dir = DEFAULT_PERSONAL_COMMANDS_DIR
    if plugins_json is None:
        plugins_json = DEFAULT_PLUGINS_JSON
    if settings_json is None:
        settings_json = DEFAULT_SETTINGS_JSON

    all_skills: list[SkillMetadata] = []
    seen_names: set[str] = set()

    def _add_skills(skills: list[SkillMetadata]) -> None:
        """Add skills to the result list, deduplicating by name."""
        for skill in skills:
            if skill.name not in seen_names:
                all_skills.append(skill)
                seen_names.add(skill.name)

    # 1. Project skills (recursive for monorepo support)
    _add_skills(
        _scan_skills_dir(
            project_dir / ".claude" / "skills", "project", recursive=True
        )
    )

    # 2. Personal skills (non-recursive per Agent Skills spec)
    _add_skills(
        _scan_skills_dir(personal_skills_dir, "personal", recursive=False)
    )

    # 3. Plugin skills (namespaced, non-recursive, active only)
    for plugin_dir, plugin_name in _get_plugin_skill_dirs(
        plugins_json, settings_json
    ):
        _add_skills(
            _scan_skills_dir(
                plugin_dir,
                "plugin",
                recursive=False,
                name_prefix=f"{plugin_name}:",
            )
        )

    # 4-5. Legacy command directories
    _add_skills(
        _scan_legacy_commands_dir(
            project_dir / ".claude" / "commands", "legacy_project"
        )
    )
    _add_skills(
        _scan_legacy_commands_dir(personal_commands_dir, "legacy_personal")
    )

    logger.info(
        "Discovered skills",
        count=len(all_skills),
        names=list(seen_names),
    )

    return all_skills


def load_skill_body(skill: SkillMetadata) -> str:
    """Load full body content without frontmatter.

    Args:
        skill: Skill metadata with file path

    Returns:
        Skill body content (empty string if file not found)
    """
    try:
        content = skill.file_path.read_text()

        # Legacy commands have no frontmatter
        if skill.source.startswith("legacy"):
            return content

        # Parse out frontmatter for modern skills
        _, body = _parse_frontmatter(content)
        return body

    except Exception as e:
        logger.error(
            "Failed to load skill body",
            skill=skill.name,
            path=str(skill.file_path),
            error=str(e),
        )
        return ""


def resolve_skill_prompt(body: str, arguments: str, session_id: str) -> str:
    """Substitute placeholders in skill body.

    Supports:
    - $ARGUMENTS: Full argument string
    - $ARGUMENTS[N]: Nth argument (space-delimited)
    - $N: Nth argument (space-delimited)
    - ${CLAUDE_SESSION_ID}: Current session ID

    Replacement order:
    1. $ARGUMENTS[N] (indexed arguments)
    2. $N (positional arguments)
    3. $ARGUMENTS (full argument string)
    4. ${CLAUDE_SESSION_ID} (session ID)

    Args:
        body: Skill body content with placeholders
        arguments: Argument string to substitute
        session_id: Session ID to substitute

    Returns:
        Body with placeholders replaced
    """
    result = body
    args_list = arguments.split() if arguments else []

    # Replace $ARGUMENTS[N] with indexed arguments
    for match in re.finditer(r"\$ARGUMENTS\[(\d+)\]", result):
        index = int(match.group(1))
        value = args_list[index] if index < len(args_list) else ""
        result = result.replace(match.group(0), value)

    # Replace $N with positional arguments
    for match in re.finditer(r"\$(\d+)", result):
        index = int(match.group(1))
        value = args_list[index] if index < len(args_list) else ""
        result = result.replace(match.group(0), value)

    # Replace $ARGUMENTS with full argument string
    result = result.replace("$ARGUMENTS", arguments)

    # Replace ${CLAUDE_SESSION_ID} with session ID
    result = result.replace("${CLAUDE_SESSION_ID}", session_id)

    return result
