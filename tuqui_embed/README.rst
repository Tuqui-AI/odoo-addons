============
Tuqui Embed
============

Permite que un origen declarado —el Tuqui de la empresa— muestre las pantallas
de este Odoo dentro de un iframe, para que quien conversa con el asistente vea
el registro del que están hablando al lado de la conversación.

**Viene apagado.** Sin el parámetro cargado, no cambia nada.

Cómo se enciende
================

En *Ajustes → Técnico → Parámetros del sistema*, crear:

::

    tuqui.embed_origins = https://tuqui.com

Varios orígenes van separados por espacios. El valor entra tal cual en
``frame-ancestors``, así que tiene que ser el origen completo (esquema + host +
puerto), sin barra final.

Para apagarlo: borrar el parámetro o dejarlo vacío.

Qué hace exactamente
====================

Dos cosas, y las dos hacen falta:

1. **Permite el frame** desde los orígenes de la lista: saca el
   ``X-Frame-Options`` y responde ``Content-Security-Policy: frame-ancestors
   'self' <lista>``. El ``'self'`` es el default de Odoo y se conserva —lo
   necesitan sus propios iframes, como el visor de PDF—: esto amplía la lista,
   no la reemplaza. Las otras directivas que Odoo haya puesto también quedan.

2. **Deja que la sesión viaje dentro del iframe**: reemite la cookie
   ``session_id`` con ``SameSite=None; Secure``, en **todas** las respuestas de
   este Odoo mientras el parámetro esté cargado — no sólo en las que vienen del
   panel. No es comodidad: el primer pedido del iframe se puede detectar por
   ``Referer``, pero llega **sin cookie** (la guardada es ``SameSite=Lax`` y no
   viaja en un frame ajeno), así que acotarlo por origen es aflojarla siempre un
   pedido tarde, cuando la navegación ya terminó en el login. Se mantienen las
   condiciones de Odoo: no se emite si la sesión no se guarda del lado del
   servidor (``auth="bearer"``, pedidos stateless) ni sobre respuestas cacheables
   en público, y lleva el mismo ``max-age`` que usa Odoo.

Sin la segunda, la primera sola no sirve: el iframe se ve, pero mostrando la
pantalla de login, porque el browser no manda una cookie ``SameSite=Lax`` en un
frame de otro sitio.

Qué se puede hacer con la cookie aflojada — medido
==================================================

Se montó el escenario completo (Odoo por HTTPS, un origen declarado que lo
muestra en un iframe, y un sitio en otro origen) y se comprobó, primero, que el
aflojamiento **ocurre**: después de pasar por el origen declarado, la sesión del
navegador queda ``SameSite=None; Secure``.

Con esa cookie, un sitio NO declarado consigue:

===================================================  ==========================
Intento                                              Resultado
===================================================  ==========================
Leer datos (``call_kw`` con ``application/json``)    **Bloqueado** — el
                                                     navegador exige preflight
                                                     CORS y Odoo no lo concede
Leer la sesión (``get_session_info``)                **Bloqueado**, por lo mismo
Escribir con POST ``form-urlencoded`` — el vector    **Rechazado: HTTP 415.**
CSRF clásico, que NO dispara preflight               Las rutas de datos de Odoo
                                                     son JSON-only
===================================================  ==========================

Verificado además contra la base: el ``write`` que intentó el sitio ajeno no
escribió nada.

**Lectura:** aflojar el ``SameSite`` no habilita leer ni escribir datos de
negocio desde otro sitio. Lo que protege no es el ``SameSite`` sino la
combinación de CORS con que las rutas de datos hablen JSON.

Lo que queda expuesto, y no se midió: las rutas ``type="http"`` del backend
(formularios como el login) sí aceptan ``form-urlencoded``. Ahí la defensa es el
token CSRF de Odoo, que sigue en pie — el atacante no puede leerlo, porque para
eso tendría que leer el HTML y CORS se lo impide.

Lo que hay que decidir antes de encenderlo
==========================================

**El punto abierto es la cookie.** ``SameSite=Lax`` es hoy una protección contra
CSRF: hace que el browser no mande la sesión en pedidos que salen de otro sitio.
Bajarla a ``None`` la saca.

Y conviene ser preciso con el alcance, porque es lo que hay que aprobar: **la
cookie sale ``SameSite=None`` en toda respuesta de este Odoo**, no sólo en las
que vienen del panel (arriba está el por qué: acotarlo por origen llega tarde).
La cookie es una sola y el browser la guarda con el último atributo que vio, así
que basta que el parámetro esté cargado para que la sesión de cualquier usuario
de esa base quede ``SameSite=None``.

Qué queda en pie igual:

- El origen tiene que estar en la lista, y lo declara un administrador.
- Odoo valida CSRF por token en los formularios del backend.
- ``httponly`` sigue puesto: el JavaScript de la página que embebe no puede leer
  la cookie.

Lo que se pierde: la red de contención para cualquier ruta que hoy dependa del
``SameSite`` y no de un token.

**La alternativa** es una cookie aparte, de vida corta, emitida sólo para las
rutas que se muestran embebidas, dejando la sesión normal intacta. Es más
trabajo y toca el manejo de sesión de Odoo, y por eso es una decisión y no un
detalle de implementación.

Notas
=====

- ``SameSite=None`` exige ``Secure``. Sobre ``http://`` el browser sólo lo acepta
  en localhost; cualquier despliegue real es HTTPS, así que no cambia nada ahí.
- El parámetro se lee en cada request, sin cachear: sacar el permiso surte
  efecto cuando el administrador lo saca, no cuando alguien reinicie el
  servidor.

Por qué se afloja siempre, y no sólo para el panel
--------------------------------------------------

La versión anterior aflojaba el ``SameSite`` **sólo cuando reconocía que el
pedido venía del origen declarado**, para acotar el alcance. No funciona, y el
motivo es un huevo y gallina medido contra un navegador de verdad:

El primer pedido que hace el iframe **sí** trae ``Referer`` del panel — se lo
puede reconocer perfectamente. Pero llega **sin cookie de sesión**, porque la
que está guardada en ese momento es la normal (``SameSite=Lax``), y esa no viaja
dentro de un frame de otro sitio. Odoo lo trata como anónimo y redirige al
login. La cookie se afloja en la respuesta… de una navegación que ya terminó
mal, y la persona ve la pantalla de login adentro del panel.

Acotar por origen era, entonces, **aflojar siempre un pedido tarde**.

Lo que esto cambia en el riesgo: de "los pedidos que vienen del panel" a "todos
los de este Odoo". Lo que NO cambia: el interruptor sigue siendo el parámetro —
sin ``tuqui.embed_origins`` cargado, el módulo no toca ninguna cookie — y la
medición de arriba sigue valiendo, porque no dependía de qué pedidos se
aflojaban sino de qué puede hacer un origen ajeno con una sesión aflojada.
