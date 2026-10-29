"""CI не должен тихо разойтись с тем, что мы проверяем руками.

Тесты локально и в GitHub Actions обязаны делать одно и то же: иначе «зелёный»
CI перестаёт что-либо гарантировать.
"""

from __future__ import annotations

from stacksentry.settings import BASE_DIR

WORKFLOW = BASE_DIR / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists() -> None:
    assert WORKFLOW.exists(), "нет .github/workflows/ci.yml"


def test_ci_runs_the_same_checks_as_documentation() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for command in (
        "ruff format --check .",
        "ruff check .",
        "mypy .",
        "pytest -q",
    ):
        assert command in text, f"CI не запускает: {command}"


def test_ci_checks_install_scripts() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for command in (
        "bash -n install.sh",
        "bash -n install/stacksentry-ctl",
        "bash -n install/stacksentry-doctor",
        "shellcheck install.sh install/stacksentry-ctl",
        "visudo -cf install/stacksentry-sudoers",
    ):
        assert command in text, f"CI не проверяет: {command}"


def test_ci_covers_promised_python_minimum() -> None:
    """README обещает Python 3.10+ — CI должен это проверять, а не верить на слово."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '"3.10"' in text, "в матрице CI нет Python 3.10"
