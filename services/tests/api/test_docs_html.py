"""The in-app API documentation pages must actually be able to render.

Both are strings of HTML in `api/app.py` that nothing exercised, and both broke
in ways that produce a BLANK PAGE rather than an error -- no exception, no
console message, HTTP 200 throughout:

* ReDoc's script was pinned to `latest`. That alias moved to a new major whose
  bundle does not initialise the `<redoc>` element on its own, so the page was
  an empty frame from whenever upstream cut that release. Swagger beside it has
  always been pinned (`swagger-ui-dist@5`) and kept working; the asymmetry was
  the whole bug.
* The theme is JSON inside an HTML attribute. A stray character makes ReDoc
  discard it -- or fail outright -- and an HTML comment was nearly committed
  INSIDE it while fixing the first problem.

Neither is visible to the route contract, which pins that `/api/redoc` exists
and says nothing about whether what it serves works.
"""

from __future__ import annotations

import json
import re

from terrapod.api.app import _REDOC_HTML, _SWAGGER_HTML


def test_the_redoc_theme_is_valid_json() -> None:
    m = re.search(r"theme='(\{.*?\})'></redoc>", _REDOC_HTML, re.S)
    assert m, "the <redoc> element no longer carries a theme attribute"
    theme = json.loads(m.group(1))  # raises, with the position, if malformed
    assert "colors" in theme and "typography" in theme


def test_the_response_sample_panel_stays_dark() -> None:
    """Light everywhere else, dark here -- the same call as the log viewers.

    ReDoc's syntax highlighting is built for a dark ground; on a light one the
    sample washes out to near-invisible. Pinned because the obvious "finish the
    light theme" edit is to lighten these two with the rest.
    """
    theme = json.loads(re.search(r"theme='(\{.*?\})'></redoc>", _REDOC_HTML, re.S).group(1))
    for value in (
        theme["rightPanel"]["backgroundColor"],
        theme["typography"]["code"]["backgroundColor"],
    ):
        r, g, b = (int(value[i : i + 2], 16) for i in (1, 3, 5))
        assert (r + g + b) / 3 < 80, f"{value} is not a dark ground"


def test_every_cdn_asset_is_pinned_to_a_version() -> None:
    """No `latest`, and nothing bare.

    A moving alias decides for us what the page runs, and the failure is a
    blank page rather than a build error, so nothing downstream notices.
    """
    urls = re.findall(r'(?:src|href)="(https://[^"]+)"', _REDOC_HTML + _SWAGGER_HTML)
    assets = [u for u in urls if u.endswith((".js", ".css"))]
    assert len(assets) >= 3, (
        f"only found {len(assets)} CDN assets; the matcher has stopped matching"
    )
    for u in assets:
        assert "/latest/" not in u and not u.endswith("@latest"), f"{u} is pinned to a moving alias"
        assert re.search(r"[@/]v?\d+(\.\d+)*(/|$|\.)", u), f"{u} carries no version"
