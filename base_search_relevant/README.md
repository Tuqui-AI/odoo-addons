# Relevance Search (`base_search_relevant`)

Búsqueda de texto rankeada sobre los modelos que un administrador activa. Sólo
depende de `base`: no instala extensiones, no crea configuraciones de búsqueda
en PostgreSQL y no toca las tablas de los modelos que indexa.

## Qué hace

- `search.relevant.config` — un modelo activado, hasta cuatro campos
  `char`/`text`/`html` para indexar y, si hace falta, un dominio que acota qué
  registros de ese modelo entran al índice.
- `search.relevant.document` — el índice, en una tabla propia (`config_id`,
  `res_id`, `layout`, `source_write_date`, `indexed_at`, `tsv` + GIN). Ninguna
  ACL la hace legible: el texto se extrae sin las reglas de nadie.
- `search.relevant.search_relevant(model, query, domain=None, limit=10)` —
  devuelve los registros ordenados por relevancia **como los ve el usuario que
  llama**: el dominio y las reglas de registro entran adentro de la consulta de
  ranking, y ningún id sale sin pasar por el `search` del usuario. El fragmento
  y los términos que coincidieron salen de la lectura del usuario, nunca del
  índice.

## Cómo se activa un modelo

Ajustes → Técnico → *Relevance Search* (o el botón que agregue el módulo que
consume la búsqueda). Se elige el modelo y los campos; el resto lo hace el cron.
Activar no bloquea la base: no se agregan columnas a tablas ajenas ni se
reescribe ninguna tabla.

## Indexar sólo una parte del modelo

El campo **Domain** de la configuración es opcional: vacío, se indexa todo el
modelo. Con un dominio, el backfill y la pasada de frescura indexan sólo los
registros que lo cumplen, y la búsqueda tampoco devuelve a los demás, ni siquiera
a los recién escritos.

El caso que lo motiva es el chatter: en `mail.message` la mayoría de los mensajes
son notificaciones automáticas y correos de seguimiento, y lo que a alguien le
sirve buscar son los comentarios de sus tareas.

| Modelo | Campos | Dominio |
|---|---|---|
| `mail.message` | `subject`, `body` | `[('message_type', '=', 'comment'), ('model', '=', 'project.task')]` |

Un registro que deja de cumplir el dominio pierde su fila en la pasada de
frescura siguiente (dejar de cumplirlo es una escritura, y la pasada mira lo
escrito hace poco). Cambiar el dominio de la configuración no reescribe nada a
mano: la corrida siguiente del cron limpia lo que sobra e indexa lo que entró.

## Qué cuesta

- Un cron cada 5 minutos, **apagado hasta que se activa el primer modelo** y
  que se apaga solo cuando no queda ninguno.
- Mientras hay backfill pendiente el cron informa avance e Odoo lo vuelve a
  correr enseguida; indexa del registro más nuevo al más viejo, por lotes de
  500 (`base_search_relevant.batch_size`).
- Una vez completo, cada corrida es una pasada de frescura por `write_date`:
  un escaneo de la tabla del modelo, sin índice posible porque la tabla no es
  de este módulo. Lo escrito después de la última pasada lo cubre la búsqueda
  leyendo esos registros en el momento; si son demasiados, o si el cron no
  corre hace más de 30 minutos, la respuesta se declara `partial`. Para que eso
  no pase inadvertido, `search.relevant.config._stale_report()` devuelve un
  aviso corto cuando el índice quedó atrasado, y un chequeo de salud lo puede
  mostrar (el módulo `tuqui` lo publica en `/tuqui/health`).
- El texto queda guardado dos veces: la columna original y el `tsvector`.

## Quién lo consume

Tuqui lo usa a través del módulo `tuqui`, que expone el contrato RPC
`tuqui.search.search_relevant` como fachada fina. **No hace falta Tuqui**: el
índice, la configuración y el método viven acá, y cualquier módulo o cliente RPC
puede llamar a `search.relevant.search_relevant`.
