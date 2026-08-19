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
    "dirty": true, "revision": 7,
    "fields": { /* valores en memoria, incl. sin guardar */ } } }
```

SPA (iframe) → Host (Odoo):

```jsonc
{ "source": "tuqui-spa", "type": "ready" }                         // montado; el host responde con auth + context
{ "source": "tuqui-spa", "type": "apply",
  "payload": { "changes": { "campo": "valor" }, "rationale": "…",
               "baseRevision": 7 } }                                // aplicar al form en memoria
{ "source": "tuqui-spa", "type": "chatter",
  "payload": { "mode": "…", "body": "…", "subject": "…" } }          // pre-carga el compositor nativo (nunca publica solo)
{ "source": "tuqui-spa", "type": "reload" }                        // releer la vista tras una escritura por atrás
{ "source": "tuqui-spa", "type": "save" }                          // guardar el form abierto (lo pidió el usuario)
```

## Contexto vivo y conflicto (`revision` / `baseRevision`)

El contexto del form no se manda una sola vez: se re-manda **mientras el usuario
edita**, para que el assistant trabaje sobre lo que está escribiendo sin guardar.

- **Cadencia:** al salir de un campo (`focusout`) con **debounce de 400 ms** —
  nunca por tecla. Una revisión por tecla infla el contexto de cada turno (costo
  de LLM y ruido) sin agregar valor. El disparador vive en
  `form_controller_patch.js`; el servicio compara contra lo último publicado y
  **no** gasta una revisión si nada cambió.
- **`revision`** (host → SPA): contador monotónico del contexto de formulario.
  Sube al cambiar de record y cada vez que los valores en memoria cambian. El
  host guarda un snapshot de los valores publicados en cada revisión (ring de
  las últimas 10).
- **`baseRevision`** (SPA → host, en `apply`): la revisión sobre la que el SPA
  razonó. El host compara, campo por campo, el valor actual contra el snapshot
  de esa revisión: **si el usuario cambió un campo después de la propuesta, ese
  campo NO se aplica** y el usuario ve por qué. Nunca se pisa en silencio.
  Si el `apply` viene sin `baseRevision` (SPA viejo) o la revisión ya salió del
  ring, no hay contra qué comparar y se aplica como antes.

## Reload de la vista (`reload`)

Cuando un turno escribe en Odoo **por atrás** (`odoo_write`, `odoo_create`,
`odoo_message_post`…), la base ya cambió pero la vista abierta sigue mostrando
lo viejo: el usuario lee "listo, actualicé los 3 contactos" arriba de tres filas
sin tocar. El SPA cierra ese hueco posteando `reload` al terminar el turno, y el
host relee los datos de la vista sin recargar la página.

**Regla dura: un formulario sucio NO se recarga.** Recargar tira los cambios sin
guardar, y perder lo que alguien estaba escribiendo es mucho peor que mostrar un
dato viejo un rato. En ese caso el host avisa y deja que el usuario guarde o
descarte. Después de recargar se re-publica el contexto, así el próximo turno
razona sobre los valores nuevos.

## Guardar el formulario (`save`)

`save` aprieta el mismo Guardar que apretaría el usuario, y existe porque
"guardalo" es lo obvio para decirle a un asistente embebido en ese formulario.

Dos cosas que NO cambia:

- **No es un atajo para que el assistant confirme sus propias propuestas.** El
  propose-then-apply existe para que el usuario revise antes de que algo se
  escriba; guardar por él le saca justo eso. La tool del backend se lo prohíbe
  explícitamente al modelo — sólo va cuando el usuario lo pide.
- **La validación sigue siendo de Odoo.** Requeridos, constraints y reglas de
  acceso aplican igual, y un guardado rechazado se reporta como rechazado en vez
  de cantar victoria.

**Nota sobre el ciclo de vida de una propuesta:** una propuesta aplicada queda en
el formulario SIN guardar, pero si el usuario **navega a otra pantalla Odoo la
guarda solo** — no la descarta. Así que "no se guarda hasta que vos guardes" es
falso; lo cierto es "hasta que guardes o te vayas de la pantalla". El prompt del
contexto de record se lo dice al modelo, y por eso también le prohíbe llamar a
`open_odoo_view` justo después de proponer: navegar commitea una propuesta que el
usuario todavía no miró.

## Selección de lista y multi-registro

Cuando el usuario tiene N filas seleccionadas, el contexto baja como
`kind:"selection"` con `count` (la selección REAL) y `resIds` **capeado en 50**
(`MAX_SELECTION_IDS`, que espeja el `batch_service.MAX_RECORDS` del backend). Si
se cortó, el payload lo dice con `truncated: true` — el agente no tiene que
creer que recibió la lista entera.

La corrida multi-registro **no pasa por `postMessage`**: el SPA llama a la tool
`run_over_selection`, que persiste un batch del lado de Tuqui y lo corre en
background. Eso es lo que hace que sobreviva a que se cierre el panel; el
resultado de cada registro queda como **nota interna en su chatter**, y el
reporte por registro se consulta con `check_selection_run`. Nada de eso necesita
que el iframe siga vivo.

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
