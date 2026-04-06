"""Tests for the repo scaffold (ticket #1)."""

import importlib
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


class TestPyprojectToml:
    """pyproject.toml must exist and be valid."""

    def test_pyproject_exists(self) -> None:
        assert (REPO_ROOT / "pyproject.toml").is_file()

    def test_pyproject_is_valid_toml(self) -> None:
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        assert data  # not empty

    def test_python_version_requirement(self) -> None:
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        requires_python = data["project"]["requires-python"]
        # Must require >=3.11
        assert "3.11" in requires_python

    def test_typer_dependency(self) -> None:
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        deps = data["project"]["dependencies"]
        assert any("typer" in d for d in deps)

    def test_dev_dependencies(self) -> None:
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        dev_deps = data["project"]["optional-dependencies"]["dev"]
        dep_names = [d.split(">=")[0].split("==")[0].split("[")[0] for d in dev_deps]
        assert "pytest" in dep_names
        assert "ruff" in dep_names
        assert "mypy" in dep_names


class TestDirectoryStructure:
    """Required directories and files must exist."""

    def test_scripts_recon_dir(self) -> None:
        assert (REPO_ROOT / "scripts" / "recon").is_dir()

    def test_templates_dir(self) -> None:
        assert (REPO_ROOT / "templates").is_dir()

    def test_claude_commands_dir(self) -> None:
        assert (REPO_ROOT / ".claude" / "commands").is_dir()

    def test_claude_settings_local(self) -> None:
        assert (REPO_ROOT / ".claude" / "settings.local.json").is_file()

    def test_gitignore_exists(self) -> None:
        assert (REPO_ROOT / ".gitignore").is_file()

    def test_readme_exists(self) -> None:
        assert (REPO_ROOT / "README.md").is_file()


class TestClaudeSettings:
    """settings.local.json must configure chief-wiggum as command source."""

    def test_command_dirs_configured(self) -> None:
        import json

        settings_path = REPO_ROOT / ".claude" / "settings.local.json"
        with open(settings_path) as f:
            settings = json.load(f)
        assert "commandDirs" in settings
        dirs = settings["commandDirs"]
        assert isinstance(dirs, list)
        assert len(dirs) > 0
        assert any("chief-wiggum" in d for d in dirs)


class TestGitignore:
    """Gitignore must cover Python, node_modules, .env, __pycache__."""

    def _gitignore_content(self) -> str:
        return (REPO_ROOT / ".gitignore").read_text()

    def test_pycache_ignored(self) -> None:
        assert "__pycache__" in self._gitignore_content()

    def test_env_ignored(self) -> None:
        content = self._gitignore_content()
        assert ".env" in content

    def test_node_modules_ignored(self) -> None:
        assert "node_modules" in self._gitignore_content()

    def test_venv_ignored(self) -> None:
        content = self._gitignore_content()
        assert ".venv" in content or "venv/" in content


class TestImports:
    """Core modules must be importable after pip install -e ."""

    def test_scripts_package_importable(self) -> None:
        mod = importlib.import_module("scripts")
        assert mod is not None

    def test_scripts_cli_importable(self) -> None:
        mod = importlib.import_module("scripts.cli")
        assert hasattr(mod, "app")

    def test_scripts_recon_package_importable(self) -> None:
        mod = importlib.import_module("scripts.recon")
        assert mod is not None
