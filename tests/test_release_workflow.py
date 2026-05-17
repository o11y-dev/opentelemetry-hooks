import re
from contextlib import redirect_stderr
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest


def _load_script(name: str):
    repo_root = Path(__file__).resolve().parent.parent
    module_path = repo_root / "scripts" / f"{name}.py"
    spec = spec_from_file_location(f"test_{name}_module", module_path)
    assert spec is not None and spec.loader is not None, f"Could not load spec for {module_path}"
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_workflow = _load_script("release_workflow")
bump_version = _load_script("bump_version")


def test_parse_semantic_release_output_ignores_already_released_version():
    output = "\n".join([
        "[20:31:38] WARNING  Token value is missing!",
        "0.1.1",
        "No release will be made, 0.1.1 has already been released!",
    ])

    assert release_workflow.parse_semantic_release_output(output) is None


def test_determine_version_prefers_untagged_pyproject_version(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.2.0"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(release_workflow, "version_tag_exists", lambda version, root=None: False)
    runner = Mock()
    version = release_workflow.determine_version(root=tmp_path, runner=runner)

    assert version == "0.2.0"
    runner.assert_not_called()


def test_determine_version_uses_semantic_release_when_tag_exists(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.1"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(release_workflow, "version_tag_exists", lambda version, root: True)
    runner = Mock(return_value=Mock(stdout="0.2.0\n", stderr="", returncode=0))

    version = release_workflow.determine_version(root=tmp_path, force="minor", runner=runner)

    assert version == "0.2.0"
    runner.assert_called_once()
    assert runner.call_args.args[0] == ["semantic-release", "version", "--print", "--minor"]


def test_version_tag_exists_checks_tag_namespace_only(monkeypatch):
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(release_workflow.subprocess, "run", run)

    assert release_workflow.version_tag_exists("0.2.0") is True
    assert run.call_args.args[0] == [
        "git",
        "show-ref",
        "--tags",
        "--verify",
        "--quiet",
        "refs/tags/v0.2.0",
    ]


def test_version_tag_exists_returns_false_when_tag_is_missing(monkeypatch):
    run = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(release_workflow.subprocess, "run", run)

    assert release_workflow.version_tag_exists("0.2.0") is False


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "pyproject.toml not found"),
        ("not = [valid", "pyproject.toml is not valid TOML"),
        ("[project]\nname = 'opentelemetry-hooks'\n", "pyproject.toml missing [project].version"),
    ],
)
def test_read_project_version_reports_common_configuration_errors(
    tmp_path: Path, contents: str | None, message: str
):
    pyproject = tmp_path / "pyproject.toml"
    if contents is not None:
        pyproject.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(message)):
        release_workflow.read_project_version(pyproject)


def test_extract_release_notes_uses_requested_section_only(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join([
            "# Changelog",
            "",
            "## 0.3.0 (unreleased)",
            "",
            "### Added",
            "- future work",
            "",
            "## 0.2.0 (2026-04-13)",
            "",
            "### Added",
            "- shipped change",
            "",
            "### Fixed",
            "- released fix",
            "",
        ]),
        encoding="utf-8",
    )

    notes = release_workflow.extract_release_notes("0.2.0", changelog)

    assert notes == "### Added\n- shipped change\n\n### Fixed\n- released fix\n"
    assert "future work" not in notes


def test_extract_release_notes_requires_existing_section(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no changelog section found for 0.2.0"):
        release_workflow.extract_release_notes("0.2.0", changelog)


def test_extract_release_notes_raises_on_empty_section(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join([
            "# Changelog",
            "",
            "## 0.3.0 (2026-04-14)",
            "",
            "## 0.2.1 (2026-04-14)",
            "",
            "### Added",
            "- shipped change",
            "",
        ]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="changelog section for 0.3.0 is empty"):
        release_workflow.extract_release_notes("0.3.0", changelog)


def test_stamp_changelog_updates_matching_unreleased_section(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join([
            "# Changelog",
            "",
            "## 0.2.1 (unreleased)",
            "",
            "- shipped change",
            "",
        ]),
        encoding="utf-8",
    )

    bump_version.stamp_changelog("0.2.1", changelog, today="2026-04-14")

    assert "## 0.2.1 (2026-04-14)" in changelog.read_text(encoding="utf-8")


def test_stamp_changelog_promotes_first_unreleased_section_when_versions_differ(
    tmp_path: Path,
):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join([
            "# Changelog",
            "",
            "## 0.3.0 (unreleased)",
            "",
            "### Added",
            "- shipped change",
            "",
        ]),
        encoding="utf-8",
    )

    bump_version.stamp_changelog("0.2.1", changelog, today="2026-04-14")

    stamped = changelog.read_text(encoding="utf-8")
    assert "## 0.2.1 (2026-04-14)" in stamped
    assert "0.3.0 (unreleased)" not in stamped


def test_stamp_changelog_skips_when_already_stamped_and_has_content(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    original = "\n".join([
        "# Changelog",
        "",
        "## 0.3.1 (unreleased)",
        "",
        "## 0.3.0 (2026-04-14)",
        "",
        "### Added",
        "- a real change",
        "",
        "## 0.2.1 (2026-04-14)",
        "",
        "### Added",
        "- older change",
        "",
    ])
    changelog.write_text(original, encoding="utf-8")

    bump_version.stamp_changelog("0.3.0", changelog, today="2026-04-14")

    result = changelog.read_text(encoding="utf-8")
    assert result == original
    assert result.count("## 0.3.0") == 1


def test_stamp_changelog_rejects_empty_section(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join([
            "# Changelog",
            "",
            "## 0.3.0 (unreleased)",
            "",
            "## 0.2.1 (2026-04-14)",
            "",
            "### Added",
            "- shipped change",
            "",
        ]),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        bump_version.stamp_changelog("0.3.0", changelog, today="2026-04-14")


def test_stamp_changelog_succeeds_with_content(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join([
            "# Changelog",
            "",
            "## 0.3.0 (unreleased)",
            "",
            "### Added",
            "- a real change",
            "",
            "## 0.2.1 (2026-04-14)",
            "",
        ]),
        encoding="utf-8",
    )

    bump_version.stamp_changelog("0.3.0", changelog, today="2026-04-14")
    assert "## 0.3.0 (2026-04-14)" in changelog.read_text(encoding="utf-8")


def test_stamp_changelog_warning_mentions_actual_path(tmp_path: Path):
    changelog = tmp_path / "nested" / "CHANGELOG.md"
    changelog.parent.mkdir()
    changelog.write_text("# Changelog\n", encoding="utf-8")
    stderr = StringIO()

    with redirect_stderr(stderr):
        bump_version.stamp_changelog("0.2.1", changelog, today="2026-04-14")

    assert str(changelog) in stderr.getvalue()
