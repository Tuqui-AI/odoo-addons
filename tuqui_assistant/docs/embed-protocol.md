# Tuqui embebido en Odoo — protocolo de embed (L1)

Contrato entre el módulo Odoo `tuqui_assistant` (host) y el SPA de Tuqui
(iframe). Spec: `Tuqui-AI/specs/.../tuqui-embebido-en-odoo.md` (task #69418).
Decisión de transporte/auth: ADR 0001 (embed sobre companion, SSO por nonce, sin
login dentro del iframe).

## Idea

El panel lateral de `tuqui_assistant` embebe el SPA de Tuqui en un `<iframe>`
(modo embed) para reusar TODA su UI (composer, micrófono, artifacts, streaming,
InlineApproval). El host y el iframe hablan por `window.postMessage`: el contexto
del formulario abierto baja, las propuestas de cambio suben y se aplican en
memoria con `record._update` (el usuario Guarda/Descarta con Odoo nativo).

## Configuración — derivada del companion (no hay system param)

La URL del embed **no** se configura con un parámetro: se deriva de la conexión
companion (ADR 0001). El método `embed_bootstrap` del modelo
`tuqui.assistant.sso.nonce` (RPC, lo llama el panel vía `getEmbedBootstrap()`)
lee el `tuqui.oauth.client` activado y devuelve `{connected, base_url, slug}`:

| Lado | Qué | Dónde |
|---|---|---|
| Odoo | Base de Tuqui a embeber | `tuqui.base_url` del oauth client (default `https://tuqui.com`). |
| Odoo | Workspace a abrir | `workspace_id_external` (slug) capturado al activar el companion. |
| Odoo | Estado de conexión | `connected = state=='active' AND hay slug`. Si es false, el panel muestra un prompt "conectá Tuqui desde Ajustes" — **sin fallback nativo**. |
| Tuqui | Permitir el framing | header CSP `frame-ancestors` en `tuqui_core/main.py` (env `EMBED_FRAME_ANCESTORS`). Reemplaza el viejo `X-Frame-Options: DENY`. |
| Tuqui | Modo embed | ruta `/embed/:slug` + flag `?embed=1` → renderiza el chat sin `AppShell`. |

El host arma el src del iframe como `{base_url}/embed/{slug}?embed=1`
(ver `embedUrl` en `panel.js`).

## Auth — SSO por nonce single-use (sin login en el iframe)

El `localStorage` del iframe está particionado (cross-site), así que no se
loguea adentro. En cambio:

1. Al recibir `ready`, el host llama `issue_for_current_user` (RPC, corre como el
   usuario logueado) → mintea un nonce single-use atado a `odoo_uid = env.uid` y
   al `client_id` del companion (TTL ~90s). El uid sale de la sesión, nunca del
   caller → un usuario sólo puede hacer SSO como sí mismo.
2. El host postea `{ source:"tuqui-odoo", type:"auth", payload:{ client_id, nonce } }`
   al iframe (una sola vez por apertura — un nonce de más se gastaría dos veces).
3. El SPA reenvía el nonce a Tuqui; Tuqui lo canjea vía
   `POST /tuqui_assistant/sso/exchange` (auth = el nonce mismo, single-use,
   atómico), recibe `{odoo_uid, client_id}`, lo mapea a un member del workspace y
   mintea un token de sesión corto. Nunca viaja un secreto: el módulo no guarda
   el client secret en plano y el nonce es la única credencial en tránsito.

## Mensajes `postMessage`

Host (Odoo) → SPA (iframe):

```jsonc
{ "source": "tuqui-odoo", "type": "auth", "payload": {
    "client_id": "…", "nonce": "…" } }                  // SSO: antes del contexto
{ "source": "tuqui-odoo", "type": "context", "payload": {
    "model": "helpdesk.ticket", "resId": 42, "displayName": "INC-0042",
    "dirty": true, "fields": { /* valores en memoria, incl. sin guardar */ } } }
```

SPA (iframe) → Host (Odoo):

```jsonc
{ "source": "tuqui-spa", "type": "ready" }                         // montado; el host responde con auth + context
{ "source": "tuqui-spa", "type": "apply",
  "payload": { "changes": { "campo": "valor" }, "rationale": "…" } } // aplicar al form en memoria
{ "source": "tuqui-spa", "type": "chatter",
  "payload": { "mode": "…", "body": "…", "subject": "…" } }          // pre-carga el compositor nativo (nunca publica solo)
```

`targetOrigin`: el host usa `new URL(base_url).origin`. El host valida que cada
mensaje entrante venga del iframe montado (`ev.source === iframe.contentWindow`)
**y** de un origin concreto que matchee el del SPA — nunca acepta `"*"` (si la
base no resuelve, descarta el mensaje). El SPA captura el origin del host de su
primer mensaje.

## Archivos

- **Odoo** (`odoo-addons` rama `feat/tuqui-assistant`):
  `static/src/panel/panel.js` (iframe + listener + SSO/auth post),
  `services/tuqui_assistant_service.js` (`getEmbedBootstrap`, `getSsoAuth`,
  `getContextPayload`, `applyProposal`→`record._update`, `proposeChatter`),
  `panel.xml` (iframe vs prompt de conexión),
  `models/tuqui_assistant_sso_nonce.py` (`embed_bootstrap`,
  `issue_for_current_user`, `redeem`), `controllers/sso.py` (`/exchange`).
- **Tuqui** (`tuqui` rama `feat/embed-mode-odoo`): `web/src/hooks/useEmbedBridge.ts`,
  `web/src/pages/EmbedChatPage.tsx`, ruta en `web/src/main.tsx`, CSP en
  `tuqui_core/main.py`, endpoint de canje del embed-token.

## Estado / pendiente (al integrar local)

- [ ] Verificar que el SPA se deja iframear con el CSP nuevo.
- [ ] Inyectar `odooContext` en el turno de Tuqui (que el agente "sepa" el registro abierto).
- [ ] Disparar `postProposalToHost(changes)` desde el resultado de la tool `propose_odoo_form_changes`.
- [ ] Navegación interna de ChatPage (`/w/:slug/...`) en modo embed: mantenerla dentro de `/embed/...`.
