# Tuqui — Odoo addons

Addons mantenidos por Tuqui-AI para extender / acompañar la conexión entre Odoo y la plataforma Tuqui.

## Contenido

| Módulo | Versiones | Qué es |
|---|---|---|
| `tuqui` | `18.0`, `19.0` | Módulo de activación / `CompanionTransport` para que un cliente con Odoo conecte Tuqui en un click. |

> Lista vigente. Cuando aparezca un módulo nuevo, agregar fila acá.

## Convención de branches

Una rama por versión soportada de Odoo (`18.0`, `19.0`, …) — no hay `main` con código. La rama `main` aloja sólo metadocs (este `README.md`, `LICENSE`, `.gitignore`, `CONTRIBUTING.md`).

Esto sigue el patrón de los repos de Adhoc (`ingadhoc/account-financial-tools`, etc.).

## Cómo se usa en desarrollo

Este repo se clona dentro del workspace OBA en `data/custom/repositories/tuqui-ai-odoo-addons` para que el devcontainer lo levante automáticamente como `addons_path`. La carpeta `custom/repositories/` es la raíz que el devcontainer suma al `addons_path` de Odoo; los repos que NO son addons (FastAPI, evals, contenido) viven en `custom/` directo. Desde el workspace de Tuqui (`~/tuqui/`) hay un symlink (`~/tuqui/odoo-addons -> ../odoo/19/data/custom/repositories/tuqui-ai-odoo-addons`) para tenerlo a tiro junto al resto de los repos de Tuqui-AI (cross-repo work, edición desde el host).

> **Nota:** después del primer clone hace falta reiniciar el devcontainer para que tome el repo nuevo dentro de `repositories/`.

## Specs y diseño

El alcance funcional vive en `Tuqui-AI/tuqui` (`docs/specs/`). Punto de partida actual:

- `docs/specs/10_draft/44-onboarding-from-odoo.md` — módulo "Tuqui" + `CompanionTransport`.
- `docs/specs/10_draft/45-onboarding-from-web.md` — flujo desde tuqui.com.

Spec orquestadora cross-repo (cuando exista): `Tuqui-AI/tuqui-workspace` → `specs/10_draft/04-modulo-tuqui-y-companion-transport.md`.

## Licencia

Privado mientras dure la fase de bring-up. Cuando se publique en `apps.odoo.com` se ajusta a LGPL-3 (estándar Odoo).
