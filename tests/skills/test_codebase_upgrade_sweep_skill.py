"""Validate the codebase-upgrade-sweep skill's SKILL.md frontmatter."""

from pathlib import Path

import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "software-development"
    / "codebase-upgrade-sweep"
    / "SKILL.md"
)


def _frontmatter() -> dict:
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert content.startswith("---"), "SKILL.md must start with ---"
    end = content.index("\n---", 3)
    block = content[3:end].strip()
    fm: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"')
    return fm


def test_skill_file_exists():
    assert SKILL_PATH.is_file(), f"missing {SKILL_PATH}"


def test_required_fields():
    fm = _frontmatter()
    for key in ("name", "description", "version", "author", "license", "platforms"):
        assert fm.get(key), f"frontmatter missing {key!r}"


def test_name_matches_directory():
    assert _frontmatter()["name"] == SKILL_PATH.parent.name


def test_description_hardline():
    desc = _frontmatter()["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars (max 60)"
    assert desc.endswith(".")


def test_author_credits_human_first():
    author = _frontmatter()["author"]
    assert author.rstrip().endswith("Hermes Agent")
    assert author.split(",")[0].strip() != "Hermes Agent"


def test_body_present_and_sections():
    content = SKILL_PATH.read_text(encoding="utf-8")
    body = content[content.index("\n---", 3) + 4 :]
    assert body.strip(), "body must not be empty"
    for section in ("## When to Use", "## Pitfalls", "## Verification"):
        assert section in body, f"missing section {section}"


@pytest.mark.parametrize("bad", ["/home/", "\\\\", ":\\\\"])
def test_no_machine_local_paths(bad):
    assert bad not in SKILL_PATH.read_text(encoding="utf-8")
