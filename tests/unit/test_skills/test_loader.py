"""Tests for skill discovery, parsing, and prompt resolution."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from src.skills.loader import (
    SkillMetadata,
    _get_plugin_skill_dirs,
    _read_enabled_plugins,
    discover_skills,
    load_skill_body,
    resolve_skill_prompt,
)


class TestDiscoverSkills:
    """Test skill discovery from filesystem."""

    def test_discovers_project_skills(self) -> None:
        """Skills in .claude/skills/ are discovered with correct metadata."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()
            skills_dir = project_dir / ".claude" / "skills" / "hello"
            skills_dir.mkdir(parents=True)

            skill_content = """---
name: hello
description: Say hello
argument-hint: <name>
user-invocable: true
allowed-tools:
  - Read
  - Write
---

Hello, $ARGUMENTS!
"""
            (skills_dir / "SKILL.md").write_text(skill_content)

            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=no_plugins,
            )

            assert len(skills) == 1
            skill = skills[0]
            assert skill.name == "hello"
            assert skill.description == "Say hello"
            assert skill.argument_hint == "<name>"
            assert skill.user_invocable is True
            assert skill.allowed_tools == ["Read", "Write"]
            assert skill.source == "project"
            assert skill.file_path == skills_dir / "SKILL.md"

    def test_discovers_personal_skills(self) -> None:
        """Skills in personal skills dir are discovered."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            personal_dir = Path(tmpdir) / "personal"
            personal_dir.mkdir()

            skill_dir = personal_dir / "greet"
            skill_dir.mkdir()
            skill_content = """---
name: greet
description: Greet user
user-invocable: true
---

Greetings!
"""
            (skill_dir / "SKILL.md").write_text(skill_content)

            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=personal_dir,
                plugins_json=no_plugins,
            )

            assert len(skills) == 1
            assert skills[0].name == "greet"
            assert skills[0].source == "personal"

    def test_skips_non_invocable_skills(self) -> None:
        """Skills with user-invocable: false are excluded."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()
            skills_dir = project_dir / ".claude" / "skills" / "internal"
            skills_dir.mkdir(parents=True)

            skill_content = """---
name: internal
description: Internal skill
user-invocable: false
---

Internal logic
"""
            (skills_dir / "SKILL.md").write_text(skill_content)

            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=no_plugins,
            )

            assert len(skills) == 0

    def test_discovers_legacy_commands(self) -> None:
        """Legacy .claude/commands/*.md files are discovered."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()
            commands_dir = project_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            (commands_dir / "test.md").write_text("Test command content")

            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=no_plugins,
            )

            assert len(skills) == 1
            assert skills[0].name == "test"
            assert skills[0].source == "legacy_project"
            assert skills[0].user_invocable is True

    def test_malformed_frontmatter_skipped(self) -> None:
        """Skills with unparseable YAML frontmatter are skipped with warning."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()
            skills_dir = project_dir / ".claude" / "skills" / "bad"
            skills_dir.mkdir(parents=True)

            skill_content = """---
name: bad
invalid: [yaml: syntax
---

Body
"""
            (skills_dir / "SKILL.md").write_text(skill_content)

            # Should not crash, just skip the skill
            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=no_plugins,
            )

            assert len(skills) == 0

    def test_deduplicates_by_name(self) -> None:
        """If same name in project and personal, project wins (first seen)."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            personal_dir = Path(tmpdir) / "personal"
            personal_dir.mkdir()

            # Project skill
            project_skill_dir = project_dir / ".claude" / "skills" / "common"
            project_skill_dir.mkdir(parents=True)
            (project_skill_dir / "SKILL.md").write_text(
                """---
name: common
description: Project version
user-invocable: true
---

Project
"""
            )

            # Personal skill with same name
            personal_skill_dir = personal_dir / "common"
            personal_skill_dir.mkdir()
            (personal_skill_dir / "SKILL.md").write_text(
                """---
name: common
description: Personal version
user-invocable: true
---

Personal
"""
            )

            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=personal_dir,
                plugins_json=no_plugins,
            )

            assert len(skills) == 1
            assert skills[0].description == "Project version"
            assert skills[0].source == "project"

    def test_no_skills_returns_empty(self) -> None:
        """Project dir with no .claude/ returns empty list."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()

            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=no_plugins,
            )

            assert skills == []


class TestLoadSkillBody:
    """Test loading skill body content."""

    def test_loads_body_without_frontmatter(self) -> None:
        """Returns content after --- markers, strips leading newlines."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            skills_dir = project_dir / ".claude" / "skills" / "test"
            skills_dir.mkdir(parents=True)

            skill_content = """---
name: test
user-invocable: true
---

Body content here
More content
"""
            skill_file = skills_dir / "SKILL.md"
            skill_file.write_text(skill_content)

            skill = SkillMetadata(
                name="test",
                description="",
                argument_hint=None,
                user_invocable=True,
                allowed_tools=[],
                source="project",
                file_path=skill_file,
            )

            body = load_skill_body(skill)

            assert body == "Body content here\nMore content\n"

    def test_loads_legacy_command_as_full_body(self) -> None:
        """Legacy .md has no frontmatter, returns entire content."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            commands_dir = project_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            command_file = commands_dir / "legacy.md"
            command_file.write_text("Full legacy command content")

            skill = SkillMetadata(
                name="legacy",
                description="",
                argument_hint=None,
                user_invocable=True,
                allowed_tools=[],
                source="legacy_project",
                file_path=command_file,
            )

            body = load_skill_body(skill)

            assert body == "Full legacy command content"


class TestResolveSkillPrompt:
    """Test skill prompt placeholder resolution."""

    def test_replaces_arguments(self) -> None:
        """$ARGUMENTS replaced with full arg string."""
        body = "Hello $ARGUMENTS!"
        result = resolve_skill_prompt(body, "world", "session123")
        assert result == "Hello world!"

    def test_replaces_positional_arguments(self) -> None:
        """$ARGUMENTS[0], $ARGUMENTS[1] replaced with split args."""
        body = "First: $ARGUMENTS[0], Second: $ARGUMENTS[1]"
        result = resolve_skill_prompt(body, "foo bar", "session123")
        assert result == "First: foo, Second: bar"

    def test_replaces_dollar_positional(self) -> None:
        """$0, $1 replaced with split args."""
        body = "First: $0, Second: $1"
        result = resolve_skill_prompt(body, "alpha beta", "session123")
        assert result == "First: alpha, Second: beta"

    def test_replaces_session_id(self) -> None:
        """${CLAUDE_SESSION_ID} replaced."""
        body = "Session: ${CLAUDE_SESSION_ID}"
        result = resolve_skill_prompt(body, "", "my-session-123")
        assert result == "Session: my-session-123"

    def test_no_arguments_leaves_empty(self) -> None:
        """$ARGUMENTS with empty string replaced with empty string."""
        body = "Args: [$ARGUMENTS]"
        result = resolve_skill_prompt(body, "", "session123")
        assert result == "Args: []"

    def test_replaces_all_placeholders(self) -> None:
        """All placeholder types can coexist."""
        body = "Args: $ARGUMENTS, First: $0, Indexed: $ARGUMENTS[1], Session: ${CLAUDE_SESSION_ID}"
        result = resolve_skill_prompt(body, "one two three", "sess-456")
        assert (
            result == "Args: one two three, First: one, Indexed: two, Session: sess-456"
        )


class TestGetPluginSkillDirs:
    """Test reading plugin install paths from installed_plugins.json."""

    def _no_settings(self, tmpdir: str) -> Path:
        """Return a path to a non-existent settings.json."""
        return Path(tmpdir) / "no_settings.json"

    def test_returns_skill_dirs_with_plugin_names(self) -> None:
        """Reads installPath and returns (skills_dir, plugin_name) tuples."""
        with TemporaryDirectory() as tmpdir:
            # Create fake plugin install paths with skills dirs
            plugin_a = Path(tmpdir) / "cache" / "mkt" / "plugin-a" / "1.0"
            plugin_a_skills = plugin_a / "skills"
            plugin_a_skills.mkdir(parents=True)

            plugin_b = Path(tmpdir) / "cache" / "mkt" / "plugin-b" / "2.0"
            plugin_b_skills = plugin_b / "skills"
            plugin_b_skills.mkdir(parents=True)

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "plugin-a@mkt": [
                                {"installPath": str(plugin_a), "version": "1.0"}
                            ],
                            "plugin-b@mkt": [
                                {"installPath": str(plugin_b), "version": "2.0"}
                            ],
                        },
                    }
                )
            )

            result = _get_plugin_skill_dirs(
                plugins_json, self._no_settings(tmpdir)
            )

            assert len(result) == 2
            dirs = [r[0] for r in result]
            names = [r[1] for r in result]
            assert plugin_a_skills in dirs
            assert plugin_b_skills in dirs
            assert "plugin-a" in names
            assert "plugin-b" in names

    def test_extracts_plugin_name_from_key(self) -> None:
        """Plugin name is extracted from 'name@marketplace' key format."""
        with TemporaryDirectory() as tmpdir:
            plugin_path = Path(tmpdir) / "cache" / "mkt" / "superpowers" / "4.0"
            (plugin_path / "skills").mkdir(parents=True)

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "superpowers@superpowers-marketplace": [
                                {"installPath": str(plugin_path), "version": "4.0"}
                            ],
                        },
                    }
                )
            )

            result = _get_plugin_skill_dirs(
                plugins_json, self._no_settings(tmpdir)
            )

            assert len(result) == 1
            assert result[0][1] == "superpowers"

    def test_skips_plugins_without_skills_dir(self) -> None:
        """Plugins whose installPath has no skills/ subdirectory are skipped."""
        with TemporaryDirectory() as tmpdir:
            plugin_path = Path(tmpdir) / "cache" / "mkt" / "no-skills" / "1.0"
            plugin_path.mkdir(parents=True)

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "no-skills@mkt": [
                                {"installPath": str(plugin_path), "version": "1.0"}
                            ],
                        },
                    }
                )
            )

            result = _get_plugin_skill_dirs(
                plugins_json, self._no_settings(tmpdir)
            )

            assert result == []

    def test_missing_plugins_json_returns_empty(self) -> None:
        """Non-existent installed_plugins.json returns empty list."""
        result = _get_plugin_skill_dirs(
            Path("/nonexistent/plugins.json"),
            Path("/nonexistent/settings.json"),
        )
        assert result == []

    def test_malformed_json_returns_empty(self) -> None:
        """Corrupted JSON returns empty list without crashing."""
        with TemporaryDirectory() as tmpdir:
            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text("not valid json {{{")

            result = _get_plugin_skill_dirs(
                plugins_json, self._no_settings(tmpdir)
            )

            assert result == []

    def test_skips_disabled_plugins(self) -> None:
        """Plugins with enabledPlugins: false are excluded."""
        with TemporaryDirectory() as tmpdir:
            # Plugin A (enabled)
            plugin_a = Path(tmpdir) / "cache" / "mkt" / "enabled" / "1.0"
            (plugin_a / "skills").mkdir(parents=True)

            # Plugin B (disabled)
            plugin_b = Path(tmpdir) / "cache" / "mkt" / "disabled" / "1.0"
            (plugin_b / "skills").mkdir(parents=True)

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "enabled@mkt": [
                                {"installPath": str(plugin_a), "version": "1.0"}
                            ],
                            "disabled@mkt": [
                                {"installPath": str(plugin_b), "version": "1.0"}
                            ],
                        },
                    }
                )
            )

            settings_json = Path(tmpdir) / "settings.json"
            settings_json.write_text(
                json.dumps(
                    {
                        "enabledPlugins": {
                            "enabled@mkt": True,
                            "disabled@mkt": False,
                        }
                    }
                )
            )

            result = _get_plugin_skill_dirs(plugins_json, settings_json)

            assert len(result) == 1
            assert result[0][1] == "enabled"

    def test_includes_all_when_no_settings(self) -> None:
        """All plugins included when settings.json has no enabledPlugins."""
        with TemporaryDirectory() as tmpdir:
            plugin_path = Path(tmpdir) / "cache" / "mkt" / "plugin" / "1.0"
            (plugin_path / "skills").mkdir(parents=True)

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "plugin@mkt": [
                                {"installPath": str(plugin_path), "version": "1.0"}
                            ],
                        },
                    }
                )
            )

            # Settings exists but no enabledPlugins key
            settings_json = Path(tmpdir) / "settings.json"
            settings_json.write_text(json.dumps({"other": "setting"}))

            result = _get_plugin_skill_dirs(plugins_json, settings_json)

            assert len(result) == 1
            assert result[0][1] == "plugin"


class TestDiscoverPluginSkills:
    """Test plugin skill discovery integrated with discover_skills."""

    def test_discovers_plugin_skills_with_namespace(self) -> None:
        """Plugin skills are namespaced as 'plugin-name:skill-name'."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()

            # Create a fake plugin with a skill
            plugin_path = Path(tmpdir) / "cache" / "mkt" / "superpowers" / "1.0"
            skill_dir = plugin_path / "skills" / "brainstorm"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                """---
name: brainstorm
description: Brainstorming skill
user-invocable: true
---

Start brainstorming!
"""
            )

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "superpowers@superpowers-marketplace": [
                                {
                                    "installPath": str(plugin_path),
                                    "version": "1.0",
                                }
                            ],
                        },
                    }
                )
            )

            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=plugins_json,
            )

            assert len(skills) == 1
            assert skills[0].name == "superpowers:brainstorm"
            assert skills[0].source == "plugin"
            assert skills[0].description == "Brainstorming skill"

    def test_plugin_skills_dedup_uses_namespaced_name(self) -> None:
        """Namespaced plugin skills don't conflict with same-named project skills."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()

            # Project skill named "review"
            proj_skill = project_dir / ".claude" / "skills" / "review"
            proj_skill.mkdir(parents=True)
            (proj_skill / "SKILL.md").write_text(
                """---
name: review
description: Project review
user-invocable: true
---

Project review body
"""
            )

            # Plugin skill also named "review" (namespaced as "myplugin:review")
            plugin_path = Path(tmpdir) / "cache" / "mkt" / "myplugin" / "1.0"
            plugin_skill = plugin_path / "skills" / "review"
            plugin_skill.mkdir(parents=True)
            (plugin_skill / "SKILL.md").write_text(
                """---
name: review
description: Plugin review
user-invocable: true
---

Plugin review body
"""
            )

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "myplugin@mkt": [
                                {
                                    "installPath": str(plugin_path),
                                    "version": "1.0",
                                }
                            ],
                        },
                    }
                )
            )

            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=plugins_json,
            )

            # Both should exist: "review" (project) and "myplugin:review" (plugin)
            assert len(skills) == 2
            names = {s.name for s in skills}
            assert names == {"review", "myplugin:review"}

    def test_multiple_plugins_discovered(self) -> None:
        """Skills from multiple plugins are all discovered with correct namespaces."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            empty_personal = Path(tmpdir) / "empty_personal"
            empty_personal.mkdir()

            # Plugin A
            plugin_a = Path(tmpdir) / "cache" / "mkt" / "alpha" / "1.0"
            skill_a = plugin_a / "skills" / "deploy"
            skill_a.mkdir(parents=True)
            (skill_a / "SKILL.md").write_text(
                """---
name: deploy
description: From plugin A
user-invocable: true
---

Deploy body
"""
            )

            # Plugin B
            plugin_b = Path(tmpdir) / "cache" / "mkt" / "beta" / "2.0"
            skill_b = plugin_b / "skills" / "test"
            skill_b.mkdir(parents=True)
            (skill_b / "SKILL.md").write_text(
                """---
name: test
description: From plugin B
user-invocable: true
---

Test body
"""
            )

            plugins_json = Path(tmpdir) / "installed_plugins.json"
            plugins_json.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {
                            "alpha@mkt": [
                                {"installPath": str(plugin_a), "version": "1.0"}
                            ],
                            "beta@mkt": [
                                {"installPath": str(plugin_b), "version": "2.0"}
                            ],
                        },
                    }
                )
            )

            skills = discover_skills(
                project_dir,
                personal_skills_dir=empty_personal,
                personal_commands_dir=empty_personal,
                plugins_json=plugins_json,
            )

            assert len(skills) == 2
            names = {s.name for s in skills}
            assert names == {"alpha:deploy", "beta:test"}
            assert all(s.source == "plugin" for s in skills)


class TestPersonalSkillsNotRecursive:
    """Test that personal skills only scan one level deep."""

    def test_skips_nested_personal_skills(self) -> None:
        """Nested directories like omc-learned/<name>/SKILL.md are not discovered."""
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            personal_dir = Path(tmpdir) / "personal"
            personal_dir.mkdir()

            # Direct child skill (should be found)
            direct_skill = personal_dir / "greet"
            direct_skill.mkdir()
            (direct_skill / "SKILL.md").write_text(
                """---
name: greet
description: Greet user
user-invocable: true
---

Hello!
"""
            )

            # Nested skill under omc-learned (should NOT be found)
            nested_skill = personal_dir / "omc-learned" / "cr"
            nested_skill.mkdir(parents=True)
            (nested_skill / "SKILL.md").write_text(
                """---
name: cr
description: Code review learned skill
user-invocable: true
---

Review code
"""
            )

            no_plugins = Path(tmpdir) / "no_plugins.json"
            skills = discover_skills(
                project_dir,
                personal_skills_dir=personal_dir,
                plugins_json=no_plugins,
            )

            assert len(skills) == 1
            assert skills[0].name == "greet"
            assert skills[0].source == "personal"
