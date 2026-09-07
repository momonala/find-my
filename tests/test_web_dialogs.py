"""A `<dialog>` that sets `display` unconditionally never leaves the page.

The browser hides a closed one with `display: none` from its own stylesheet, so
any rule setting `display` on the dialog itself overrides that: the dialog stays
laid out, invisible at `opacity: 0`, on top of whatever is under it -- and eats
every click meant for those controls. It has caught this codebase twice, both
times looking like an unrelated control having stopped working.
"""

import re
from pathlib import Path

import pytest

from src.core.paths import REPO_ROOT

_WEB = REPO_ROOT / "src" / "web"

# `.thing[open] { display: grid }` is the correct form, and so is a rule for a
# child of a dialog. This looks only at rules for the dialog element itself.
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.MULTILINE)
_SETS_DISPLAY = re.compile(r"(?:^|;)\s*display\s*:", re.MULTILINE)


def _dialog_classes() -> set[str]:
    """Every class on a `<dialog>` in the templates."""
    found: set[str] = set()
    for template in _WEB.rglob("*.html"):
        for tag in re.findall(r"<dialog\b[^>]*>", template.read_text()):
            attribute = re.search(r'class="([^"]*)"', tag)
            if attribute:
                found.update(attribute.group(1).split())
    return found


def _stylesheets() -> list[Path]:
    return sorted(_WEB.rglob("*.css"))


def test_the_templates_still_have_dialogs_to_check() -> None:
    """Guards the test itself: a rename that empties the set must not pass silently."""
    assert _dialog_classes()
    assert _stylesheets()


@pytest.mark.parametrize("stylesheet", _stylesheets(), ids=lambda path: path.name)
def test_no_rule_sets_display_on_a_closed_dialog(stylesheet: Path) -> None:
    classes = _dialog_classes()
    offenders = []
    for selector, body in _RULE.findall(stylesheet.read_text()):
        selector = selector.strip()
        if "[open]" in selector or not _SETS_DISPLAY.search(body):
            continue
        # Only the dialog element itself: `.photo-detail .thing` is a child.
        for name in classes:
            if re.search(rf"\.{re.escape(name)}\s*(?:,|$)", selector):
                offenders.append(f"{selector} sets display on a closed .{name}")

    assert not offenders, (
        "A closed <dialog> given a `display` stays on the page and swallows clicks. "
        "Scope the rule to [open]:\n  " + "\n  ".join(offenders)
    )
