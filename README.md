# Tuqui — Odoo addons

Addons mantenidos por Tuqui-AI para extender / acompañar la conexión entre Odoo y la plataforma Tuqui.

## Contenido

| Módulo | Versiones | Qué es |
|---|---|---|
| `tuqui` | `18.0`, `19.0` | Módulo de activación / `CompanionTransport` para que un cliente con Odoo conecte Tuqui en un click. |
| `tuqui_assistant` | `19.0` | Panel embebido del asistente Tuqui en el backend de Odoo (chat contextual + propose-then-apply sobre el formulario activo). |
| `tuqui_mcp` | `16.0`, `17.0`, `18.0`, `19.0` | Módulo data-only para Odoo Apps. Registra los parámetros de conexión con Tuqui. Compatible con Odoo Online. |

> Lista vigente. Cuando aparezca un módulo nuevo, agregar fila acá.

## Convención de branches

Una rama por versión soportada de Odoo (`19.0`, `18.0`, …) — no hay `main`. La rama default es la versión activa más reciente (hoy `19.0`). Patrón estándar en repos de addons Odoo.

## Cómo se usa en desarrollo

Para **correr** los módulos hace falta un clone dentro del devcontainer OBA de
la versión que corresponda — uno por versión, con la rama de esa versión.
Tener el repo en el workspace de Tuqui no alcanza: no entra al `addons_path`, y
el fallo es silencioso.

El checkout del workspace de Tuqui sirve para leer, editar cambios simples y
trabajo cross-repo.

## Specs y diseño

Las specs del ecosistema Tuqui-AI viven en [`Tuqui-AI/specs`](https://github.com/Tuqui-AI/specs)
— repo dedicado, con su propio `AGENTS.md`.

## Licencia

LGPL-3.0 — estándar de los addons Odoo. Ver `LICENSE`.
