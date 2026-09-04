"""Handing Tuqui the key it needs to verify our signatures, without a teardown.

**The problem this solves.** The outbound signing key is minted during
activation, and activation refuses to run again while the connection is live
("Disconnect first to re-activate"). So every database that connected before
signed events existed — which is all of them — could only get a key by tearing
the connection down: rotating the client secret, killing the live tokens, and
walking an admin back through a screen where they can pick the wrong workspace.
A migration step that risky, for a credential neither side needs a human to
agree on, is the wrong shape.

**Why handing it over this channel is safe.** The caller has already presented
an OAuth access token minted from the client secret this database issued to
Tuqui. That token can read the whole ERP through ``/tuqui/rpc``. A key whose
only power is to *prove events came from here* is not an escalation on top of
that — and Tuqui has to hold it anyway, because verifying an HMAC needs the key
itself.

What this route deliberately does not do is rotate. If a key already exists it
is returned as-is: rotating would invalidate the events sitting in the queue
right now, which is the opposite of what a durable queue is for.
"""

import json
import logging

from odoo import http
from odoo.http import Response, request

from .oauth import verify_access_token
from .rpc import _bearer_token

_LOG = logging.getLogger(__name__)


class TuquiSigningKeyController(http.Controller):
    @http.route(
        "/tuqui/companion/signing-key",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        readonly=False,
    )
    def signing_key(self, **_kwargs):
        env = request.env
        token = _bearer_token()
        if not token or not verify_access_token(env, token):
            return _json({"error": {"code": "unauthorized", "message": "Missing or invalid bearer token"}}, 401)

        client = env["tuqui.oauth.client"].sudo()._get_singleton()
        if not client or client.state != "active":
            # A disconnected database hands out nothing. The token check above
            # already fails in that state, so this is belt and braces.
            return _json({"error": {"code": "forbidden", "message": "This database is not connected"}}, 403)

        minted = not client.event_signing_key
        key = client._ensure_signing_key()
        if minted:
            _LOG.info("tuqui.signing_key: minted on request for client_id %s", client.client_id)
        return _json({"event_signing_key": key, "minted": minted}, 200)


def _json(payload, status):
    return Response(json.dumps(payload), content_type="application/json", status=status)
