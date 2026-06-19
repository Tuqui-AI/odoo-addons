# Tuqui embebido en Odoo — protocolo de embed (L1)

Contrato entre el módulo Odoo `tuqui_assistant` (host) y el SPA de Tuqui
(iframe). Spec: `Tuqui-AI/specs/.../tuqui-embebido-en-odoo.md` (task #69418).

## Idea

El panel lateral de `tuqui_assistant` embebe el SPA de Tuqui en un `<iframe>`
(modo embed) para reusar TODA su UI (composer, micrófono, artifacts, streaming,
InlineApproval). El host y el iframe hablan por `window.postMessage`: el contexto
del formulario abierto baja, las propuestas de cambio suben y se aplican en
memoria con `record._update` (el usuario Guarda/Descarta con Odoo nativo).

## Configuración

| Lado | Qué | Dónde |
|---|---|---|
| Odoo | URL del SPA a embeber | system parameter `tuqui_assistant.spa_url` (ej. `http://localhost:5173`). Sin esto, el panel cae al shell de chat nativo. |
| Tuqui | Permitir el framing | header CSP `frame-ancestors` en `tuqui_core/main.py` (env `EMBED_FRAME_ANCESTORS`, default cubre Odoo local). Reemplaza el viejo `X-Frame-Options: DENY`. |
| Tuqui | Modo embed | ruta `/embed/:slug` + flag `?embed=1` → renderiza el chat sin `AppShell`. |

El host arma el src del iframe como `{spa_url}?embed=1`.

## Mensajes `postMessage`

Host (Odoo) → SPA (iframe):

```jsonc
{ "source": "tuqui-odoo", "type": "context", "payload": {
    "model": "helpdesk.ticket", "resId": 42, "displayName": "INC-0042",
    "dirty": true, "fields": { /* valores en memoria, incl. sin guardar */ } } }
```

SPA (iframe) → Host (Odoo):

```jsonc
{ "source": "tuqui-spa", "type": "ready" }                         // montado; el host responde con "context"
{ "source": "tuqui-spa", "type": "apply",
  "payload": { "changes": { "campo": "valor" }, "rationale": "…" } } // aplicar al form en memoria
```

`targetOrigin`: el host usa `new URL(spa_url).origin`; el SPA captura el origin
del host de su primer mensaje. El host valida `ev.source === iframe.contentWindow`
y el origin.

## Archivos

- **Odoo** (`odoo-addons` rama `feat/tuqui-assistant`): `static/src/panel/panel.js`
  (iframe + listener), `services/tuqui_assistant_service.js` (`getSpaUrl`,
  `getContextPayload`, `applyProposal`→`record._update`), `panel.xml` (iframe vs
  fallback nativo).
- **Tuqui** (`tuqui` rama `feat/embed-mode-odoo`): `web/src/hooks/useEmbedBridge.ts`,
  `web/src/pages/EmbedChatPage.tsx`, ruta en `web/src/main.tsx`, CSP en
  `tuqui_core/main.py`.

## Estado / pendiente (al integrar local)

- [ ] Verificar que el SPA se deja iframear con el CSP nuevo (el objetivo del spike).
- [ ] Inyectar `odooContext` en el turno de Tuqui (que el agente "sepa" el registro abierto).
- [ ] Disparar `postProposalToHost(changes)` desde el resultado de la tool `propose_odoo_form_changes`.
- [ ] Auth: el `localStorage` del iframe está particionado (cross-site) → pasar token por postMessage o loguear dentro del iframe (el spike puede loguear adentro).
- [ ] Navegación interna de ChatPage (`/w/:slug/...`) en modo embed: mantenerla dentro de `/embed/...`.
