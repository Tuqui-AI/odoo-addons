============
Tuqui Embed
============

Lets a declared origin —the company's Tuqui— show this Odoo's screens inside an
iframe, so that whoever is talking to the assistant sees the record under
discussion next to the conversation.

**It ships switched off.** With the parameter unset, nothing changes.

**It does one thing: it allows the frame.** It does not touch the session
cookie, it does not touch Odoo's session handling, it adds no routes. The user's
session showing up inside the iframe is not this module's doing — it follows
from how the panel is served, and that is under "The precondition" below.

How to switch it on
===================

Under *Settings → Technical → System Parameters*, create:

::

    tuqui.embed_origins = https://odoo-acme.tuqui.com

Several origins go space-separated. The value goes verbatim into
``frame-ancestors``, so it has to be the full origin (scheme + host + port),
with no trailing slash. It is validated on save: no wildcards, no scheme
without a host, no ``http://`` outside ``localhost``/``127.0.0.1``.

To switch it off: clear the parameter or leave it empty.

What it does, exactly
=====================

**It allows the frame** from the origins on the list: it drops
``X-Frame-Options`` and answers
``Content-Security-Policy: frame-ancestors 'self' <list>``.

And it **switches off onboarding tours when the screen is shown framed**, which
is not an extra but the difference between "it shows" and "it can be used" — see
"The tour crash" below.

The ``'self'`` IS ALWAYS THERE. Odoo frames its own pages same-origin —the PDF
and text viewer, the report preview— and a list without ``'self'`` leaves those
blank for the WHOLE database the moment the switch goes on. Odoo's default is,
precisely, ``frame-ancestors 'self'``: this widens the list, it does not replace
it.

And the CSP is **completed**, not replaced. ``set_csp`` puts
``default-src 'none'`` on every ``image/*`` response (``odoo/http.py``), which is
what sandboxes an SVG uploaded as an attachment. Writing over the header left
that SVG running script in Odoo's origin — a hole with nothing to do with
embedding, present on every response.

The precondition: the panel has to be served on the SAME SITE
=============================================================

Allowing the frame is not enough on its own: if the panel is on another
**site**, Odoo's session cookie (``SameSite=Lax``) does not travel into the
iframe, and the user sees the login screen even though they are already logged
into Odoo.

The way out is **not** to loosen the cookie (see the next section): it is for
the panel and this Odoo to share a site. ``SameSite`` is defined per **site**
(registrable domain), not per origin, so ``https://tuqui.com`` and
``https://odoo-acme.tuqui.com`` are the same site and the ordinary cookie walks
into the iframe on its own.

In practice that means Tuqui serves this Odoo under a host of its own
—``odoo-<workspace>.tuqui.com``, forwarding to the customer's Odoo— and the
browser only ever talks to that host.

**Measured** (real Chrome, with Odoo's default cookie: ``SameSite=Lax``, no
``Secure``, no partitioning):

======================================================  ==================
Case                                                    Result
======================================================  ==================
Opening the panel while already logged into Odoo        Comes in logged in
Reopening it in a new tab                               Comes in logged in
After closing and reopening the browser                 Comes in logged in
Redirect loops                                          None
======================================================  ==================

What that trial does NOT cover: the proxy itself. The two hosts were set up on
the same site to isolate the cookie question; host rewriting, the bus WebSocket
upgrade and asset traffic through Tuqui remain untested end to end.

Why the cookie is NOT loosened — two measured vectors
=====================================================

An earlier version of this module reissued the session with ``SameSite=None`` so
it would travel to a panel on another site. It was dropped, and it is worth
writing down why, because it is the path one drifts back to.

Loosening ``SameSite`` opens two channels **CORS does not cover** (tested live
against the dev Odoo 19, with an administrator cookie and
``Origin: https://evil.example.com``):

- **Cross-origin WebSocket.** ``/websocket`` is ``auth="public", cors="*"``
  (``addons/bus/controllers/websocket.py``); the session downgrade that would
  protect against this only kicks in when ``ODOO_BUS_PUBLIC_SAMESITE_WS`` is
  set, and it is not set by default. Handshake OK, and the socket receives the
  user's live bus — a WebSocket is readable by JS cross-origin, with no
  preflight and no credentials gate.
- **``/web/become``: zero-click superuser escalation.**
  ``web/controllers/home.py`` — ``auth='user'``, **GET**, no token, and for a
  user who is already ``_is_system()`` it does ``session.uid = SUPERUSER_ID``.
  With ``SameSite=None``, an ``<img src=".../web/become">`` on any page a
  logged-in admin visits escalates them with no click. Reproduced in real
  Chrome, comparing the admin's ``session_id`` and not merely its presence:
  **before the mitigation the foreign site walked off with the real session**.
  Same family: ``/mail/unfollow``, ``/web/hook``.

``Partitioned`` (CHIPS) was tried as a mitigation and **it does not work for
this design**. It closes the ``<img>`` (measured before/after), but it **breaks
the panel**: reopening it in a new tab sends Odoo into an infinite loop between
the requested screen and ``/web/login`` (``ERR_TOO_MANY_REDIRECTS``).
Discriminator measured in both directions — without ``Partitioned`` that same
navigation returns 200 and comes in logged in. The loop's mechanism remains
**unexplained**: the trace suggests the browser sends the cookie to one route
and not the other, but that reading comes from a header introspection which, in
the same run, returned empty for one of the two requests — so it is a
hypothesis, not a measurement.

With the panel same-site there is nothing to loosen, so neither vector ever
opens. That is what leaves this module with no pending security decision.

The tour crash: an Odoo bug, and its workaround
===============================================

With an onboarding tour in progress, this web client inside a cross-origin
iframe **kills the browser tab**: the tour pointer reaches for the parent
document, that throws ``SecurityError`` in a loop (59 counted within seconds)
and takes the renderer's memory with it.

**The tour does not start in the panel: it starts in the everyday Odoo.** The
user goes in, the tour begins and leaves its state in ``localStorage``; later
they open the panel —same origin, same ``localStorage``— and the tour is RESUMED
inside the iframe.

**The root cause is one line of Odoo.** ``web_tour/tour_service.js`` already
tries to prevent this: it starts and resumes tours inside
``if (!window.frameElement)``. But ``window.frameElement`` returns ``null`` when
the parent is of ANOTHER origin, so the guard holds precisely in the case it
meant to prevent. The condition that does work cross-origin is
``window.top !== window.self``. **This belongs upstream.**

Meanwhile, the module covers it from both sides:

- **Server** (``models/ir_http.py``): if the request is a frame's navigation
  (``Sec-Fetch-Dest: iframe``), the ``session_info`` goes out with
  ``tour_enabled`` and ``current_tour`` off. This stops a tour from STARTING
  inside the panel.
- **Client** (``static/src/no_tours_when_framed.js``): the door that matters.
  ``tourState.getCurrentTour()`` returns ``null`` inside a frame, so there is
  nothing to resume. Resumption reads ``localStorage``, not the
  ``session_info``, which is why the server side alone was not enough.

**Neither half consults ``tuqui.embed_origins``**, and that is deliberate. The
crash is Odoo's and happens to anyone framing this web client, whether this
module authorised it or not. The client half cannot read a server parameter
anyway, so gating the server half on the switch left the two halves of one fix
under different rules — the sort of asymmetry that stays invisible until
somebody frames this Odoo by another route.

**Measured, with the pair that discriminates** (a pending tour in both cases):

====================  ===========================  ==============
Where                 Tour starts/resumes?         Crashes?
====================  ===========================  ==============
Top-level (own Odoo)  **Yes** — onboarding intact  No
Inside the panel      **No**                       No, 0 errors
====================  ===========================  ==============

Before the fix: 59 ``SecurityError`` and a dead tab. After: 0 errors and the
tour bundle is not even downloaded.

Two decisions worth not reverting without reading this:

- **``tour_service`` is not removed from the registry**, though it would be more
  direct: the onboarding widget and the POS call ``useService("tour_service")``
  and would blow up on render. Keeping the service alive also matters beyond
  politeness — Tuqui drives the pointer on purpose inside the panel.
- **The user's progress is not erased.** ``null`` is returned only inside the
  frame; ``localStorage`` is left intact, so in their everyday Odoo the tour
  carries on where they left it. There is a test pinning exactly that.

And faking ``window.frameElement`` so Odoo's own guard would work was discarded:
it is one line, but ``website`` and the editor USE that element
(``dispatchEvent``, ``ownerDocument``), so it would have traded a tour crash for
breakage elsewhere.

The other thing an embedded Odoo has to not do
==============================================

The Odoo being shown may have ``tuqui_assistant`` installed — and that panel
reopens itself if it was open before. Tuqui shows Odoo, that Odoo opens its own
Tuqui, that Tuqui restores its panel with Odoo, and so on: every level loads a
full web client and **the whole browser goes down**, not just the tab.

That guard lives in ``tuqui_assistant``
(``static/src/nested_guard.js``), not here. An earlier version lived in this
module and could not hold: to run before that panel it had to declare
``('before', 'tuqui_assistant/…')``, which made this module impossible to
install without the assistant. It belongs with the behaviour being suppressed —
the assistant deciding not to mount — rather than with the module that opened
the door.

The risk that does remain, and it is much smaller
=================================================

With the panel on the same site, ``SameSite=Lax`` travels between pages of that
site. So the risk moved from "any site on the internet" to "any page under our
own domain".

**Measured, with the pair that discriminates.** Two pages request
``/web/become`` with an ``<img>``, and the ``Cookie`` header the browser
attached is compared against the admin's exact ``session_id`` (that "some
session_id" showed up proves nothing: Odoo gives any visitor an anonymous
session):

=========================================  ==========================================
Origin of the attacking page               Did it carry the admin's session?
=========================================  ==========================================
Another site (``evil.localhost``)          **No** — no cookie reached it
Same site (``evil.localtest.me``)          **Yes** — with the admin's ``session_id``
=========================================  ==========================================

That makes the deployment condition concrete: **nothing served as a document
under Tuqui's domain may be content we do not control.**

**And this is where the design deserves a second look**, because the proxy
itself is what puts foreign documents there. Each customer's Odoo is served as a
document under ``odoo-<workspace>.tuqui.com``, and those hosts are same-site
with each other and with the panel. A page authored inside customer A's Odoo —a
``website`` page is the obvious one— can issue
``<img src="https://odoo-B.tuqui.com/web/become">``, and a ``_is_system()`` user
of B with a live session becomes superuser there, with no click. Attachments are
not a vector (``Stream.get_response`` gives them ``default-src 'none'`` by
default); document routes that render user-authored HTML with no CSP are.

A cheaper variant needs no ``website`` at all: any document under
``*.tuqui.com`` can set a cookie with ``Domain=.tuqui.com``. ``httponly``
prevents reading, not writing a new, wider-scoped one — that is session fixation
against the other workspaces and against the panel.

Closing that class properly means the site not being ``tuqui.com`` and not being
shared between workspaces — a dedicated domain with a wildcard Public Suffix
List entry, where a workspace's panel and its proxied Odoo are same-site with
each other while two workspaces are not. **That decision belongs to the proxy,
not to this module**, and it should be taken before switching this on for a
customer.

Reasoning is recorded here, not measured by this module: the artifacts published
by Tuqui are not a vector, because they run in an iframe whose ``sandbox`` does
NOT include ``allow-same-origin`` (opaque origin, so it is "same site" with
nothing) and their public route returns JSON rather than a document served on
Tuqui's origin. Read from the ``tuqui-py`` code
(``web/src/lib/artifacts/sandbox.ts``, ``tuqui_core/artifacts/public_router.py``),
not measured in a browser.

What contains the rest:

- ``frame-ancestors`` still bounds **who** may frame: being same-site is not
  enough, you have to be on the list the administrator declared.
- Odoo's data routes are JSON-only, so a ``<form>`` POST does not write. That is
  pinned in ``tests/test_csrf_invariant.py``, because it is a property of Odoo
  we depend on and it could change without anything turning red. Note it does
  **not** cover ``/web/become``, which is a GET and needs no body.
- ``httponly`` is still set (Odoo sets it; this module does not touch it).

Decisions taken
===============

- **The permission lives in this module and not in ``tuqui``.** The companion is
  server-only and ships on 18.0 and 19.0; this feature cannot be switched on
  without the tour fix, which is 19-only. A switch the module carrying it cannot
  safely allow does not belong there. One feature, one module.
- **Is the module needed at all, or could the proxy strip the header?** The
  proxy could, but the tour fix is not header work and can only live inside
  Odoo — so the module is needed regardless. On top of that, the customer's
  administrator **declares** who may frame their Odoo, and that consent lives
  with the owner of the data; and a misconfigured proxy is not enough on its own
  to expose anyone.
- **Validating that the declared origin is same-site** is not done here on
  purpose. It requires the Public Suffix List, which is a living dataset that
  would rot inside a module installed on every customer's Odoo — and it is a
  property of the Tuqui deployment, which chooses both hosts, not of the
  customer's Odoo. The guardrail belongs in Tuqui, where the panel is
  configured.

Notes
=====

- The parameter is read on every request, but ``get_param`` is ormcached, so on
  a multi-worker deployment revoking the permission may take a while to reach
  workers that already had it cached. Measured: with the value changed by
  another process, this module kept answering with the previous one until a
  restart. If revocation has to be immediate, the invalidation has to be forced.
- A ``frame-ancestors`` carrying the list is NOT the same as allowing anyone: it
  is the only thing that separates this from removing clickjacking protection.
