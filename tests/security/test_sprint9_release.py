"""Regression tests for Sprint 9 release artefacts (version + docs + CI)."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# ``tomllib`` is stdlib from Python 3.11 onward. On 3.10 (still in the CI
# matrix) we fall back to the ``tomli`` shim, which exposes the same API.
if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover - only hit on Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Version alignment: pyproject.toml, app.__version__, FastAPI app.version
# must all advertise the same string. A release tag that does not match
# pyproject is caught by the release workflow; this test catches drift
# between the three in-repo spots.
# ---------------------------------------------------------------------------


def test_sprint9_version_alignment(client) -> None:
    from app import __version__
    from app.main import app as fastapi_app

    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    declared = pyproject["project"]["version"]

    assert __version__ == declared, (
        f"app.__version__ ({__version__}) != pyproject version ({declared})"
    )
    assert fastapi_app.version == declared

    # And it lands in the OpenAPI document unchanged.
    schema = client.get("/v1/openapi.json").json()
    assert schema["info"]["version"] == declared


def test_sprint9_version_is_1_x_or_higher() -> None:
    from app import __version__

    major = int(__version__.split(".", 1)[0])
    assert major >= 1, "v1.0.0 release should not regress below 1.x"


# ---------------------------------------------------------------------------
# CHANGELOG: the current version must be the top [version] block so the
# release workflow's awk-based extractor grabs the right notes.
# ---------------------------------------------------------------------------


def test_sprint9_changelog_mentions_current_version() -> None:
    from app import __version__

    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text()
    assert f"## [{__version__}]" in changelog, (
        f"CHANGELOG.md does not declare a section for {__version__}"
    )


def test_sprint9_changelog_current_block_comes_before_prior_versions() -> None:
    from app import __version__

    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text()
    current_marker = f"## [{__version__}]"
    prior_marker = "## [0.1.0]"
    # Both must be present; the current marker must come first.
    assert current_marker in changelog
    assert prior_marker in changelog
    assert changelog.index(current_marker) < changelog.index(prior_marker)


# ---------------------------------------------------------------------------
# Release workflow: must exist, run on tag push, and reference the Docker
# build + GHCR login + verify job.
# ---------------------------------------------------------------------------


def test_sprint9_release_workflow_shape() -> None:
    workflow = _REPO_ROOT / ".github" / "workflows" / "release.yml"
    assert workflow.exists(), "Release workflow is missing"
    parsed = yaml.safe_load(workflow.read_text())
    # YAML parses ``on`` as a boolean True in some loaders; accept either.
    triggers = parsed.get("on") or parsed.get(True)
    assert triggers is not None, "workflow has no trigger section"
    push_triggers = triggers.get("push", {}).get("tags", [])
    assert any(t.startswith("v") for t in push_triggers), "release workflow must trigger on v* tags"
    jobs = parsed["jobs"]
    for job in ("verify", "wheel", "docker", "github-release"):
        assert job in jobs, f"release workflow missing job '{job}'"


# ---------------------------------------------------------------------------
# Post-audit matrix: every top-level audit bucket must be listed.
# ---------------------------------------------------------------------------


def test_sprint9_post_audit_doc_covers_every_bucket() -> None:
    doc = (_REPO_ROOT / "docs" / "post-audit.md").read_text()
    for bucket in (
        "## Critique",
        "## Élevé",
        "## Moyen",
        "## API / Contrat",
        "## Tests",
        "## DevEx / Ops",
        "## Archi",
        "## Open / deferred",
    ):
        assert bucket in doc, f"post-audit matrix missing section {bucket!r}"


def test_sprint9_post_audit_doc_has_status_column() -> None:
    doc = (_REPO_ROOT / "docs" / "post-audit.md").read_text()
    # Sanity: the matrix uses an emoji status column. A release that
    # silently drops it would be trivial to miss at review time.
    for marker in ("✅", "🟡"):
        assert marker in doc, f"status marker {marker!r} absent from post-audit doc"
