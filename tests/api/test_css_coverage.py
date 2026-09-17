"""Every class a template reaches for must exist in app.css.

The redesign landed markup for 36 classes before the stylesheet had rules for
them, and nothing caught it -- the pages just rendered unstyled. This test
parses every template's `class="..."` attributes and every class selector in
app.css, and fails if a template uses a class app.css never defines.

Jinja expressions (an attribute value containing `{` or `}`, e.g.
`class="pill--{{ level_tone.get(e.level, 'pending') }}"`) are skipped
entirely rather than tokenized -- splitting them on whitespace would produce
garbage "class names" out of the expression's own words.
"""

import re
from pathlib import Path

import immich_gphotos

WEB_DIR = Path(immich_gphotos.__file__).parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
APP_CSS = WEB_DIR / "static" / "app.css"

CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')
CSS_CLASS_SELECTOR_RE = re.compile(r"\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)")


def _classes_used_in_templates() -> set[str]:
    used: set[str] = set()
    for template in TEMPLATES_DIR.glob("*.html"):
        text = template.read_text()
        for attr_value in CLASS_ATTR_RE.findall(text):
            if "{" in attr_value or "}" in attr_value:
                continue  # a Jinja expression, not a literal class list
            used.update(attr_value.split())
    return used


def _classes_defined_in_css() -> set[str]:
    css = APP_CSS.read_text()
    # Strip comments so a class name mentioned only in prose (like this
    # file's own docstring-style comments) can't count as "defined".
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return set(CSS_CLASS_SELECTOR_RE.findall(css))


def test_every_template_class_is_defined_in_app_css():
    used = _classes_used_in_templates()
    defined = _classes_defined_in_css()
    missing = used - defined
    assert not missing, f"classes referenced by templates but not defined in app.css: {sorted(missing)}"
