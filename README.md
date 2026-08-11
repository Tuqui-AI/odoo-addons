# Tuqui — Odoo addons

Addons mantenidos por Tuqui-AI para extender / acompañar la conexión entre Odoo y la plataforma Tuqui.

## Contenido

| Módulo | Versiones | Qué es |
|---|---|---|
| `tuqui` | `18.0`, `19.0` | Módulo de activación / `CompanionTransport` para que un cliente con Odoo conecte Tuqui en un click. |

> Lista vigente. Cuando aparezca un módulo nuevo, agregar fila acá.

## Convención de branches

Una rama por versión soportada de Odoo (`19.0`, `18.0`, …) — no hay `main`. La rama default es la versión activa más reciente (hoy `19.0`). Patrón estándar en repos de addons Odoo.

## Cómo se usa en desarrollo

En esta rama no hay Python: lo que hay es data-only (XML) más los assets de la
description del listing de Odoo Apps. Editar y hacer trabajo cross-repo se puede
desde el checkout del workspace de Tuqui.

Lo que no se puede desde ahí es **cargar el módulo en un Odoo**: no entra al
`addons_path`, y el fallo es silencioso. Para eso hace falta un clone de esta
rama en una instancia de la 16 — uno por versión, porque un checkout no sirve
para dos.

## Specs y diseño

Las specs del ecosistema Tuqui-AI viven en [`Tuqui-AI/specs`](https://github.com/Tuqui-AI/specs)
— repo dedicado, con su propio `AGENTS.md`.

## Licencia

LGPL-3.0 — estándar de los addons Odoo. Ver `LICENSE`.
