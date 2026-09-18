"""Every class a template (or app.js) reaches for must exist in app.css.

The redesign landed markup for 36 classes before the stylesheet had rules for
them, and nothing caught it -- the pages just rendered unstyled. This test
parses every template's `class="..."` attributes, app.js's `class="..."` and
`className = "..."` string literals, and every class selector in app.css, and
fails if either source uses a class app.css never defines.

Jinja expressions (an attribute value containing `{` or `}`, e.g.
`class="pill--{{ level_tone.get(e.level, 'pending') }}"`) are skipped
entirely rather than tokenized -- splitting them on whitespace would produce
garbage "class names" out of the expression's own words.

app.js builds the application's only modal via `dialog.innerHTML`, which is
exactly the kind of markup a templates-only glob misses -- `.dialog__title`
and `.dialog__text` slipped through undefined there for that reason.
"""

import re
from pathlib import Path

import immich_gphotos

WEB_DIR = Path(immich_gphotos.__file__).parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
APP_CSS = WEB_DIR / "static" / "app.css"
APP_JS = WEB_DIR / "static" / "app.js"

CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')
# A `className = "..."` assignment immediately followed by `+` (e.g.
# `el.className = "toast toast--" + tone;`) is building a class list out of a
# runtime value, not a literal one -- skipped the same way a Jinja expression
# is, rather than registering the dangling "toast--" prefix as a real class.
CLASSNAME_ASSIGN_RE = re.compile(r'className\s*=\s*"([^"]*)"(\s*\+)?')
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


def _classes_used_in_app_js() -> set[str]:
    used: set[str] = set()
    text = APP_JS.read_text()
    for attr_value in CLASS_ATTR_RE.findall(text):
        if "{" in attr_value or "}" in attr_value:
            continue
        used.update(attr_value.split())
    for value, concatenated in CLASSNAME_ASSIGN_RE.findall(text):
        if concatenated:
            continue  # built via string concatenation, not a literal class list
        used.update(value.split())
    return used


def _classes_defined_in_css() -> set[str]:
    css = APP_CSS.read_text()
    # Strip comments so a class name mentioned only in prose (like this
    # file's own docstring-style comments) can't count as "defined".
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return set(CSS_CLASS_SELECTOR_RE.findall(css))


def test_every_template_class_is_defined_in_app_css():
    used = _classes_used_in_templates() | _classes_used_in_app_js()
    defined = _classes_defined_in_css()
    missing = used - defined
    assert not missing, (
        f"classes referenced by templates/app.js but not defined in app.css: {sorted(missing)}"
    )


def test_hidden_attribute_wins_over_component_display_rules():
    """Templates use the `hidden` *attribute* (not a `.hidden` class) to hide
    elements -- e.g. wizard.html's `.panel--done` and `.shell-command`,
    dashboard.html's `.meter`. A normal author `display` declaration (like
    `.panel--done { display: grid }`) beats the UA stylesheet's
    `[hidden] { display: none }` by specificity, so those elements stay
    visible despite carrying `hidden`. Without an author-level `[hidden]`
    rule that outranks every component's `display`, this fails silently in
    every browser -- nothing else in this suite renders the page, so this is
    the only check that would catch it.
    """
    css = APP_CSS.read_text()
    css_without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    match = re.search(r"\[hidden\]\s*{([^}]*)}", css_without_comments)
    assert match, "app.css has no [hidden] rule to win over component display rules"
    body = match.group(1)
    assert re.search(r"display\s*:\s*none\s*!important", body), (
        "[hidden] must set `display: none !important` to beat any component's "
        "own `display` declaration regardless of source order"
    )


def test_reduced_motion_block_is_last_in_app_css():
    """The `prefers-reduced-motion: reduce` block must stay last in app.css.

    Its `*, *::before, *::after` rules carry `!important`, so those three
    always win regardless of source order -- but `.btn:hover`/`.btn:active`
    inside the same block does not carry `!important`, so it only wins if
    the block is the last thing in the file. A rule appended after it that
    targets the same selector with equal specificity would silently defeat
    reduced-motion for that rule. This test only checks placement; it can't
    catch a rule inserted *before* the block that defeats it by other means.
    """
    css = APP_CSS.read_text()

    starts = [m.start() for m in re.finditer(r"@media\s*\(prefers-reduced-motion", css)]
    assert starts, "app.css has no @media (prefers-reduced-motion ...) block"
    last_start = starts[-1]

    # Walk forward from the block's own opening brace, matching braces, to
    # find where this block actually ends.
    open_brace = css.index("{", last_start)
    depth = 0
    end = None
    for i, ch in enumerate(css[open_brace:], start=open_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end is not None, "unbalanced braces in the reduced-motion block"

    trailing = css[end:]
    # Comments are allowed to follow (e.g. none currently do, but a trailing
    # file-end comment shouldn't count as "more CSS"); actual rules are not.
    trailing_without_comments = re.sub(r"/\*.*?\*/", "", trailing, flags=re.DOTALL)
    assert not trailing_without_comments.strip(), (
        "the last @media (prefers-reduced-motion ...) block must be the very "
        "last rule in app.css so it always wins; found more CSS after it: "
        f"{trailing_without_comments.strip()[:200]!r}"
    )


def test_a_hover_color_rule_excludes_btn_anchors():
    """`a:hover` must never be an unscoped selector on its own.

    `a.btn` strips the underline from anchor-buttons, but no `.btn:hover`
    (or `a.btn:hover`) colour rule exists. An unscoped `a:hover { color:
    ... }` rule has specificity (0,1,1), which beats `.btn--primary` /
    `.btn--ghost` at (0,1,0), so it silently recolours button labels on
    hover -- on `.btn--primary` this drops contrast to roughly 1.2:1,
    making the label disappear under the cursor. `a:not(.btn):hover` keeps
    the same specificity while excluding button anchors, so it's the
    required form.
    """
    css = APP_CSS.read_text()
    css_without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    offending = []
    for selector_text, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css_without_comments):
        for selector in (s.strip() for s in selector_text.split(",")):
            if selector == "a:hover":
                offending.append(body.strip())

    assert not offending, (
        "found an unscoped `a:hover` rule that would apply to `.btn` "
        f"anchors too; use `a:not(.btn):hover` instead: {offending}"
    )
