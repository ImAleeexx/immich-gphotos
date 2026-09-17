"""Guard against inline scripts that touch `window.igp` before it exists.

`base.html` loads `/static/app.js` (which defines `window.igp`) with
`defer`, so it runs after the document is parsed but before
`DOMContentLoaded` fires. An inline `<script>` in the body runs *during*
parsing -- before `app.js` -- so any inline script that references `igp.`
outside a `DOMContentLoaded` handler will throw a ReferenceError the moment
the page loads in a real browser. Nothing else in this suite runs a
browser, so this static check is the only thing that would have caught
`dashboard.html` calling `igp.subscribeStatus(render)` at the top level.
"""

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "immich_gphotos" / "web" / "templates"

# Matches inline <script> bodies only -- a `<script src="...">` tag (like
# base.html's `/static/app.js` include) has no body to check.
INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)


def _inline_scripts(path: Path) -> list[str]:
    return INLINE_SCRIPT_RE.findall(path.read_text())


@pytest.mark.parametrize("path", sorted(TEMPLATES_DIR.glob("*.html")), ids=lambda p: p.name)
def test_inline_scripts_only_touch_igp_after_dom_content_loaded(path):
    for script in _inline_scripts(path):
        if "igp." in script:
            assert "DOMContentLoaded" in script, (
                f"{path.name} has an inline script that references `igp.` but never waits "
                "for DOMContentLoaded -- app.js is loaded with `defer` and runs after "
                "inline scripts, so igp is not yet defined when this script executes."
            )
