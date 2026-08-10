# AGENTS.md — Tuqui odoo-addons

> Convención cross-vendor ([agents.md](https://agents.md)) — lectura primaria para Claude Code, OpenAI Codex, Cursor, OpenCode, Gemini CLI, Aider y otros agentes compatibles.

## Qué es este repo

Los addons Odoo mantenidos por Tuqui-AI: el connector que un cliente instala en
su Odoo para conectarse a Tuqui, y el asistente embebido en el backend.

| Módulo | Versiones | Qué es |
|---|---|---|
| `tuqui` | `18.0`, `19.0` | Connector companion: activación en un click + `CompanionTransport` (el transporte que usa el backend de Tuqui contra el Odoo del cliente). |
| `tuqui_assistant` | `19.0` | Panel embebido del asistente en el backend de Odoo (chat contextual + propose-then-apply sobre el formulario activo). Depende de `tuqui`. |
| `tuqui_mcp` | `16.0`–`19.0` | Data-only para Odoo Apps: registra los parámetros de conexión. Compatible con Odoo Online. |

Es un repo de **módulos Odoo**, no de la app. El backend (FastAPI) vive en
`Tuqui-AI/tuqui`; este repo es la contraparte del lado del cliente.

## Resources

- `oba-19`, `oba-18` — las versiones donde se corren y prueban estos módulos.
- `tuqui` — el workspace: el backend contra el que habla el connector, y el
  resto de los repos del ecosistema. Vínculo bidireccional (el workspace
  declara este repo).

## Ramas

**Una rama por versión de Odoo soportada** (`16.0`, `17.0`, `18.0`, `19.0`) —
no hay `main`. La default es la versión activa más reciente (hoy `19.0`).
Patrón estándar de repos de addons Odoo.

Consecuencia práctica: **un checkout no sirve para dos versiones.** Cuando un
fix aplica a más de una, va una branch y un PR por rama de versión.

## Convenciones

- **Commits**: español, prefijo conventional con módulo entre paréntesis —
  `fix(tuqui_assistant): …`, `feat(tuqui): …`, `test(tuqui): …`. Cuando el
  cambio sale de una tarea, el número va al final: `(#71708)`.
- **Sin co-autores IA.** Ningún `Co-Authored-By:` de un agente, ningún footer
  "generado por IA". El commit es del humano que dirige el cambio.
- **Branches**: `<prefijo>/<descripcion-kebab>` (`feat/`, `fix/`, `chore/`,
  `docs/`). No aplica el patrón OBA `[ver]-[t|h]-[rec_id]-[autor]`.
- **PRs**: same-repo, base = la rama de versión que corresponda (no `main`, no
  existe). Merge con **Rebase and merge** desde la UI. Sin mergebot.
- **Versionado de manifests**: `<version-odoo>.<x>.<y>.<z>`. Bumpear en el
  `__manifest__.py` del módulo tocado cuando el cambio es funcional.
- **Licencia**: LGPL-3.0, estándar de addons Odoo.

## Specs

Las specs del ecosistema Tuqui-AI viven en [`Tuqui-AI/specs`](https://github.com/Tuqui-AI/specs)
— repo dedicado, con su propio `AGENTS.md`. No hay specs en este repo ni en
`Tuqui-AI/tuqui`.

## Calidad

`pre-commit` corre en CI (`.github/workflows/pre-commit.yml`). Correlo local
antes de pushear si tocaste Python o XML.
