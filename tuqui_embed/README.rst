============
Tuqui Embed
============

Permite que un origen declarado —el Tuqui de la empresa— muestre las pantallas
de este Odoo dentro de un iframe, para que quien conversa con el asistente vea
el registro del que están hablando al lado de la conversación.

**Viene apagado.** Sin el parámetro cargado, no cambia nada.

**Hace una sola cosa: permite el frame.** No toca la cookie de sesión, no toca
el manejo de sesión de Odoo, no agrega rutas. Que la sesión del usuario aparezca
adentro del iframe no es trabajo de este módulo — es consecuencia de cómo se
sirve el panel, y eso está en "La precondición" más abajo.

Cómo se enciende
================

En *Ajustes → Técnico → Parámetros del sistema*, crear:

::

    tuqui.embed_origins = https://odoo-acme.tuqui.com

Varios orígenes van separados por espacios. El valor entra tal cual en
``frame-ancestors``, así que tiene que ser el origen completo (esquema + host +
puerto), sin barra final. Se valida al guardar: no se aceptan comodines, un
esquema sin host, ni ``http://`` fuera de ``localhost``/``127.0.0.1``.

Para apagarlo: borrar el parámetro o dejarlo vacío.

Qué hace exactamente
====================

**Permite el frame** desde los orígenes de la lista: saca el ``X-Frame-Options``
y responde ``Content-Security-Policy: frame-ancestors 'self' <lista>``.

Y **apaga los tours de onboarding cuando la pantalla se muestra embebida**, que
no es un agregado sino la diferencia entre "se ve" y "se puede usar" — ver
"El crash de los tours" más abajo.

El ``'self'`` VA SIEMPRE. Odoo embebe sus propias páginas en iframes del mismo
origen —el visor de PDF y de texto, el preview de reportes— y una lista sin
``'self'`` los deja en blanco para TODA la base en cuanto se prende el switch.
El default de Odoo es, justamente, ``frame-ancestors 'self'``: esto amplía la
lista, no la reemplaza.

Y la CSP se **completa**, no se reemplaza. ``set_csp`` le pone
``default-src 'none'`` a toda respuesta ``image/*`` (``odoo/http.py``), que es lo
que sandboxea un SVG subido como adjunto. Sobrescribir el header dejaba ese SVG
ejecutando script en el origen de Odoo — un agujero que no tiene nada que ver
con embeber, y que aparecía en todas las respuestas.

La precondición: el panel tiene que servirse en el MISMO SITIO
==============================================================

Permitir el frame no alcanza por sí solo: si el panel está en otro **sitio**, la
cookie de sesión de Odoo (``SameSite=Lax``) no viaja adentro del iframe, y el
usuario ve la pantalla de login aunque ya esté logueado en Odoo.

La salida **no** es aflojar la cookie (ver la sección siguiente): es que el panel
y este Odoo compartan sitio. ``SameSite`` se define por **sitio** (dominio
registrable), no por origen, así que ``https://tuqui.com`` y
``https://odoo-acme.tuqui.com`` son el mismo sitio y la cookie normal entra al
iframe sola.

En la práctica eso significa que Tuqui sirve este Odoo bajo un host propio
—``odoo-<workspace>.tuqui.com``, reenviando al Odoo del cliente— y el navegador
sólo habla con ese host.

**Medido** (Chrome real, con la cookie por default de Odoo: ``SameSite=Lax``,
sin ``Secure``, sin partición):

======================================================  ==================
Caso                                                    Resultado
======================================================  ==================
Abrir el panel estando ya logueado en Odoo              Entra logueado
Reabrirlo en una pestaña nueva                          Entra logueado
Después de cerrar y reabrir el navegador                Entra logueado
Bucles de redirección                                   Ninguno
======================================================  ==================

Lo que ese ensayo NO cubre: el proxy en sí. Se montaron los dos hosts en el
mismo sitio para aislar la pregunta de la cookie; la reescritura de host, el
upgrade del WebSocket del bus y el tráfico de assets a través de Tuqui siguen
sin probarse punta a punta.

Por qué NO se afloja la cookie — dos vectores medidos
=====================================================

Una versión anterior de este módulo reemitía la sesión con ``SameSite=None``
para que viajara a un panel de otro sitio. Se descartó, y conviene que quede
escrito por qué, porque es el camino al que uno vuelve solo.

Aflojar el ``SameSite`` abre dos canales que **CORS no cubre** (probado en vivo
contra el Odoo 19 de dev, con cookie de administrador y
``Origin: https://evil.example.com``):

- **WebSocket cross-origin.** ``/websocket`` es ``auth="public", cors="*"``
  (``addons/bus/controllers/websocket.py``); el downgrade de sesión que
  protegería de esto sólo actúa si ``ODOO_BUS_PUBLIC_SAMESITE_WS`` está
  seteada, y no lo está ni por default. Handshake OK, y el socket recibe el bus
  en vivo del usuario — un WebSocket es legible por JS cross-origin, sin
  preflight ni gate de credenciales.
- **``/web/become``: escalada a superusuario zero-click.**
  ``web/controllers/home.py`` — ``auth='user'``, **GET**, sin token, y para un
  usuario que ya es ``_is_system()`` hace ``session.uid = SUPERUSER_ID``. Con
  ``SameSite=None``, un ``<img src=".../web/become">`` en cualquier página que
  visite un admin logueado lo escala sin un clic. Reproducido en Chrome real,
  comparando el ``session_id`` del admin y no su mera presencia: **antes del
  mitigante el sitio ajeno se llevaba la sesión real**. Misma familia:
  ``/mail/unfollow``, ``/web/hook``.

``Partitioned`` (CHIPS) se probó como mitigante y **no sirve para este diseño**.
Cierra el ``<img>`` (medido antes/después), pero **rompe el panel**: al
reabrirlo en una pestaña nueva, Odoo entra en un bucle infinito entre la
pantalla pedida y ``/web/login`` (``ERR_TOO_MANY_REDIRECTS``). Discriminador
medido en las dos direcciones — sin ``Partitioned`` esa misma navegación
devuelve 200 y entra logueada. El mecanismo del bucle quedó **sin explicar**: la
traza sugiere que el navegador manda la cookie a una ruta y no a la otra, pero
esa lectura viene de una introspección de headers que en la misma corrida
devolvió vacío para uno de los dos pedidos, así que es hipótesis, no medición.

Con el panel same-site no hay nada que aflojar, así que ninguno de los dos
vectores se abre. Eso es lo que hace que este módulo no tenga una decisión de
seguridad pendiente.

El crash de los tours: un bug de Odoo, y su workaround
======================================================

Con un tour de onboarding en curso, este webclient dentro de un iframe de otro
origen **le crashea la pestaña**: el puntero del tour busca el documento del
padre, eso tira ``SecurityError`` en bucle (59 contados en pocos segundos) y se
lleva la memoria del renderer.

**El tour no arranca en el panel: arranca en el Odoo de siempre.** El usuario
entra, el tour empieza y deja su estado en ``localStorage``; después abre el
panel —mismo origen, mismo ``localStorage``— y el tour se REANUDA adentro del
iframe.

**La causa raíz es una línea de Odoo.** ``web_tour/tour_service.js`` ya intenta
evitarlo: arranca y reanuda tours dentro de ``if (!window.frameElement)``. Pero
``window.frameElement`` devuelve ``null`` cuando el padre es de OTRO origen, así
que la guarda se cumple justo en el caso que quería prevenir. La condición que
sí funciona cross-origin es ``window.top !== window.self``. **Corresponde
reportarlo upstream.**

Mientras tanto, el módulo lo tapa por los dos lados:

- **Servidor** (``models/ir_http.py``): si el pedido es la navegación de un
  iframe (``Sec-Fetch-Dest: iframe``), el ``session_info`` sale con
  ``tour_enabled`` y ``current_tour`` apagados. Evita que un tour ARRANQUE
  dentro del panel.
- **Cliente** (``static/src/no_tours_when_framed.js``): la puerta que importa.
  ``tourState.getCurrentTour()`` devuelve ``null`` dentro de un frame, así que
  no hay nada que reanudar. La reanudación lee ``localStorage``, no el
  ``session_info``, y por eso el lado servidor solo no alcanzaba.

**Medido, con el par que discrimina** (un tour pendiente en los dos casos):

====================  ===========================  ==============
Dónde                 ¿Arranca/reanuda el tour?    ¿Crashea?
====================  ===========================  ==============
Top-level (su Odoo)   **Sí** — onboarding intacto  No
Dentro del panel      **No**                       No, 0 errores
====================  ===========================  ==============

Antes del arreglo: 59 ``SecurityError`` y pestaña muerta. Después: 0 errores y
el bundle del tour ni se descarga.

Dos cosas que se decidieron y conviene no revertir sin leer esto:

- **No se saca ``tour_service`` del registry**, aunque sería más directo: el
  widget de onboarding y el POS hacen ``useService("tour_service")`` y
  reventarían al renderizar.
- **No se borra el progreso del usuario.** Se devuelve ``null`` sólo dentro del
  frame; el ``localStorage`` queda intacto, así que en su Odoo de siempre el
  tour sigue donde lo dejó. Hay un test que fija justamente que no se borre.

Y se descartó falsificar ``window.frameElement`` para que la guarda de Odoo
funcionara sola: es una línea, pero hay código de ``website`` y del editor que
USA ese elemento (``dispatchEvent``, ``ownerDocument``), así que habría
cambiado un crash de tours por roturas en otro lado.

El riesgo que sí queda, y es mucho más chico
============================================

Con el panel en el mismo sitio, ``SameSite=Lax`` viaja entre páginas de ese
sitio. O sea: el riesgo se mudó de "cualquier sitio de internet" a "cualquier
página bajo nuestro propio dominio" — un subdominio comprometido o mal
apuntado. Bajo nuestro control, pero no cero.

**Medido, con el par que discrimina.** Dos páginas piden ``/web/become`` con un
``<img>``, y se mira el header ``Cookie`` que el navegador adjuntó comparado
contra el ``session_id`` exacto del admin (que "haya algún session_id" no
prueba nada: Odoo le da una sesión anónima a cualquier visitante):

=========================================  ==========================================
Origen de la página que ataca              ¿Se llevó la sesión del admin?
=========================================  ==========================================
Otro sitio (``evil.localhost``)            **No** — no le llegó ninguna cookie
Mismo sitio (``evil.localtest.me``)        **Sí** — con el ``session_id`` del admin
=========================================  ==========================================

Eso vuelve concreta la condición de despliegue: **nada que se sirva como
documento bajo el dominio de Tuqui puede ser contenido que no controlemos.**

Hoy eso se cumple, y conviene que quede anotado porque pasa a ser una
propiedad a preservar: los artifacts publicados **no** son un vector, porque
corren en un iframe cuyo ``sandbox`` NO incluye ``allow-same-origin`` (origen
opaco, o sea que no es "mismo sitio" con nada) y su ruta pública devuelve JSON,
no un documento servido en el origen de Tuqui. Leído del código de
``tuqui-py`` (``web/src/lib/artifacts/sandbox.ts``,
``tuqui_core/artifacts/public_router.py``), no medido en un navegador.

Lo que lo contiene:

- ``frame-ancestors`` sigue acotando **quién** puede embeber: no alcanza con ser
  del mismo sitio, hay que estar en la lista que declaró el administrador.
- Las rutas de datos de Odoo son JSON-only, así que un ``<form>`` POST no
  escribe. Eso está fijado en ``tests/test_csrf_invariante.py``, porque es una
  propiedad de Odoo de la que dependemos y podría cambiar sin que nada se
  ponga rojo.
- ``httponly`` sigue puesto (Odoo lo pone; este módulo no lo toca).

Lo que queda abierto para el review
===================================

- **¿Hace falta este módulo, o el header lo saca el proxy?** Siendo Tuqui el que
  proxea, podría sacar el ``X-Frame-Options`` y reescribir la CSP al pasar, sin
  instalar nada en el Odoo del cliente. A favor de este módulo: el
  administrador del cliente **declara** explícitamente quién puede embeber su
  Odoo, y ese consentimiento vive con el dueño del dato en vez de decidirlo
  Tuqui por su cuenta. Es una decisión de producto, no de código.
- **Validar que el origen declarado sea same-site** con este Odoo sería un
  buen guardarraíl (hoy se puede declarar un origen de otro sitio y el panel va
  a mostrar el login sin explicar por qué). No se implementó a propósito:
  saber si dos hosts comparten dominio registrable exige la Public Suffix List,
  y una aproximación naíf del tipo "comparar las dos últimas etiquetas" da mal
  justo en los dominios que más usamos (``.com.ar``).

Notas
=====

- El parámetro se lee en cada request, pero ``get_param`` está ormcacheado, así
  que en un despliegue multi-worker sacar el permiso puede tardar en surtir
  efecto en los workers que ya lo tenían cacheado. Medido: con el valor cambiado
  por otro proceso, este módulo siguió respondiendo con el anterior hasta
  reiniciar. Si la revocación tiene que ser inmediata, hay que forzar la
  invalidación.
- Un ``frame-ancestors`` con la lista NO es lo mismo que permitir a cualquiera:
  es lo único que distingue esto de sacar la protección de clickjacking.
