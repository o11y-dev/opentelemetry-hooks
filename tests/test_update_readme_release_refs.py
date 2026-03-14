import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "update_readme_release_refs.py"


def test_updates_all_release_tag_references(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "\n".join([
            "pip install git+https://github.com/o11y-dev/opentelemetry-hooks.git@v0.1.0",
            "pip install git+https://github.com/o11y-dev/opentelemetry-hooks.git@v1.2.3",
            "git tag v9.9.9",
        ]),
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, str(SCRIPT), str(readme), "2.4.6"],
        check=True,
    )

    assert readme.read_text(encoding="utf-8") == "\n".join([
        "pip install git+https://github.com/o11y-dev/opentelemetry-hooks.git@v2.4.6",
        "pip install git+https://github.com/o11y-dev/opentelemetry-hooks.git@v2.4.6",
        "git tag v9.9.9",
    ])


def test_usage_error_for_missing_arguments():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "usage: update_readme_release_refs.py" in proc.stderr
