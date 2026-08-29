"""Regression tests for vulnerabilities that were live in this codebase.

Both were found by attacking a running deployment, not by reading the code.
They exist here so they cannot come back silently — a change that reintroduces
either one fails CI.
"""

from __future__ import annotations

import pytest

# Chains that escalate from "renders a banner" to reading interpreter state.
# A plain jinja2.Environment renders every one of these happily; the last four
# are the standard published routes to remote code execution.
SSTI_PAYLOADS = [
    pytest.param('{{ "".__class__.__mro__ }}', id="mro-walk"),
    pytest.param('{{ "".__class__.__mro__[1].__subclasses__() }}', id="subclasses-rce"),
    pytest.param("{{ cycler.__init__.__globals__ }}", id="globals-rce"),
    pytest.param("{{ self.__init__.__globals__ }}", id="self-globals"),
    pytest.param("{{ ''.__class__.__base__.__subclasses__() }}", id="base-subclasses"),
]


@pytest.mark.parametrize("payload", SSTI_PAYLOADS)
def test_ssti_payloads_are_blocked(client, auth, payload):
    """The renderer takes template *source* from callers, so it must sandbox it."""
    r = client.post(
        "/v1/image/render",
        json={"template": payload, "data": {}, "width": 200, "height": 100},
        headers=auth,
    )
    assert r.status_code == 422, f"SSTI payload was not blocked: {payload}"


def test_bare_internal_attribute_leaks_nothing(client, auth, stub_renderer):
    """A single unsafe attribute resolves to Undefined rather than raising.

    The sandbox only raises once something is *done* with that Undefined, so
    `{{ "".__class__ }}` renders successfully — but it must render to nothing.
    A plain Environment emits "<class 'str'>" here, which is the first rung of
    the ladder to the payloads above.
    """
    r = client.post(
        "/v1/image/render",
        json={"template": '<p>{{ "".__class__ }}</p>', "data": {}},
        headers=auth,
    )
    assert r.status_code == 200
    rendered = stub_renderer[0]["files"][0][1][1].decode()
    assert rendered == "<p></p>"
    for leak in ("class", "str", "object", "type"):
        assert leak not in rendered


def test_sandbox_error_does_not_leak_internals(client, auth):
    """Naming the blocked attribute just helps someone map the sandbox edge."""
    r = client.post(
        "/v1/image/render",
        json={"template": '{{ "".__class__.__mro__ }}', "data": {}},
        headers=auth,
    )
    detail = r.json()["detail"]
    for leak in ("__class__", "__mro__", "__globals__", "Traceback", "jinja2"):
        assert leak not in detail


# --------------------------------------------------------------------------
# Legitimate templates must keep working. A sandbox that breaks real use is
# just an outage.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "template,data",
    [
        ("<h1>{{ title }}</h1>", {"title": "Hello"}),
        ("{% for i in items %}<li>{{ i }}</li>{% endfor %}", {"items": [1, 2, 3]}),
        ("{{ name|upper }}", {"name": "ada"}),
        ("{% if x %}<b>yes</b>{% else %}no{% endif %}", {"x": True}),
        ("{{ n + 1 }}", {"n": 41}),
        ("{{ items|length }} found", {"items": ["a", "b"]}),
    ],
    ids=["variable", "loop", "filter", "conditional", "arithmetic", "length"],
)
def test_ordinary_templates_still_render(client, auth, template, data):
    r = client.post(
        "/v1/image/render",
        json={"template": template, "data": data},
        headers=auth,
    )
    assert r.status_code == 200


def test_caller_data_is_autoescaped(client, auth, stub_renderer):
    """Customer data must not be able to inject markup into the render."""
    client.post(
        "/v1/image/render",
        json={"template": "<p>{{ x }}</p>", "data": {"x": "<script>alert(1)</script>"}},
        headers=auth,
    )
    rendered = stub_renderer[0]["files"][0][1][1].decode()
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_oversized_template_rejected(client, auth):
    r = client.post(
        "/v1/image/render",
        json={"template": "<p>x</p>" * 60_000, "data": {}},
        headers=auth,
    )
    assert r.status_code == 413


# --------------------------------------------------------------------------
# SSRF containment is enforced by Gotenberg's deny-lists, which live in
# docker-compose.yml. Assert the configuration exists — a silent deletion of
# those flags reopens access to loopback, RFC1918 and the Tailscale range.
# --------------------------------------------------------------------------

def test_compose_declares_ssrf_denylists():
    import pathlib

    compose = (pathlib.Path(__file__).parent.parent / "docker-compose.yml").read_text()

    assert "--chromium-deny-list" in compose
    assert "--libreoffice-deny-list" in compose, "office docs fetch linked content too"

    for cidr_marker in (
        "127",            # loopback
        r"10\.",          # RFC1918
        r"192\.168",      # RFC1918
        r"169\.254",      # link-local / cloud metadata
        "100",            # CGNAT — the range Tailscale uses
    ):
        assert cidr_marker in compose, f"deny-list no longer covers {cidr_marker}"

    # Gotenberg's own default blocks file:// outside its working directory.
    # Overriding the flag without re-adding this silently drops it.
    assert "^file:" in compose
