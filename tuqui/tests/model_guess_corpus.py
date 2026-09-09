"""Every model name production asked for and did not get, and what it meant.

This is not a hand-picked sample. ``GUESSES`` is the full set of distinct names
that came back as ``Unknown model`` from the Tuqui backend between 2026-06-01
and 2026-09-08, and ``REGISTRY`` is the COMPLETE registry of the Odoo those
calls hit — a suggestion is only as good as the wrong answers it beat.

Two rounds of the same mistake got it here, both worth remembering:

* The first version was eight names chosen because they were known to work,
  over a registry written from memory. It scored 8/8 and hid that the ranker
  was right about half the time on real input.
* The second kept the real names but trimmed the registry to the 279 models
  that "could compete". Word weights are ``1.0 / occurrences``, so trimming
  inflated every weight while ``_MODEL_MIN_SEGMENTS`` stayed absolute: the same
  corpus read 54/61 trimmed and 49/61 whole, and the five it "won" were
  confident wrong answers, not silence.

Both times the sample flattered the thing it measured. Keep the registry whole.

Expected value per guess:
    str  — that model must come back first
    set  — any of those is defensible; the guess is genuinely ambiguous
    None — nothing may come back: every candidate would point somewhere else
"""

from odoo.addons.tuqui.tests.model_guess_registry import MODELS

# Assigned rather than re-exported straight from the import: the name is what
# this file means by it — the pool every guess gets scored against.
REGISTRY = MODELS

GUESSES = {
    # ── The pull request family. 82 calls, six invented names, one session
    # trying three of them in four seconds. The model is `saas.pull.request`.
    "adhoc.pull.request": "saas.pull.request",
    "adhoc.pull": "saas.pull.request",
    "adhoc.pull_request": "saas.pull.request",
    "pull.request": "saas.pull.request",
    "github.pull.request": "saas.pull.request",
    "git.pull.request": "saas.pull.request",
    "project.pull.request": "saas.pull.request",
    "saas.pull": "saas.pull.request",
    "saas.database.pull.request": "saas.pull.request",
    "runbot.pull": "saas.pull.request",
    "runbot.branch.pull": "saas.pull.request",
    "runbot.build.pull": "saas.pull.request",
    "runbot.merge.pull": "saas.pull.request",
    "runbot.merge.pull_requests": "saas.pull.request",
    "project.task.pull": "saas.pull.request",
    "project.task.pull.request": "saas.pull.request",
    # `pr` is not a word of the vocabulary; staying on the task is honest.
    "project.task.pr": {"project.task", "saas.pull.request"},
    # ── A dot where the model has an underscore. Second most frequent guess
    # in the whole corpus, and invisible to a ranker that splits on dots only.
    "saas.database.custom.domain": "saas.database.custom_domain",
    "saas.custom.domain": "saas.database.custom_domain",
    "saas.domain": "saas.database.custom_domain",
    "saas.database.custom_domain.wizard": "saas.database.custom_domain",
    "helpdesk.ticket.customer.note": "helpdesk.ticket.customer_note",
    # ── A name invented over a family that does exist.
    "saas.upgrade.request": {"saas.upgrade.line.request.run", "saas.upgrade.line.request.log"},
    "saas.upgrade.request.run": "saas.upgrade.line.request.run",
    "saas.upgrade.line.request": {"saas.upgrade.line.request.run", "saas.upgrade.line.request.log"},
    "saas.database.module": {"adhoc.module.module", "adhoc.module", "saas.database"},
    "saas.module": {"adhoc.module", "adhoc.module.module"},
    "adhoc.module.version": {"saas.odoo.version", "adhoc.module", "saas.odoo.major_version"},
    "adhoc.major.version": {"saas.odoo.major_version", "major.version.change"},
    "saas.major.version": {"saas.odoo.major_version", "major.version.change"},
    "saas.app.version": {"helm.app.version", "helm.app.version.value"},
    "odoo.version": "saas.odoo.version",
    "saas.helm.app.value": "helm.app.version.value",
    "saas.helm.app.version.value": "helm.app.version.value",
    "saas.k8s.cluster": "saas.cluster",
    "saas.kpi": "saas.database.kpi",
    "saas.repository": {"adhoc.module.repository", "saas.odoo.version.repository"},
    "saas.repositories": {"adhoc.module.repository", "saas.repositories.update"},
    "saas.odoo.project": {"saas.database", "saas.odoo.version"},
    "saas.client": {"saas_client.dashboard", "saas.upgrade.client.data"},
    "project.main.database": "saas.database",
    "helpdesk.ticket.tag": "helpdesk.tag",
    "crm.lead.tag": "crm.tag",
    "stock.orderpoint": "stock.warehouse.orderpoint",
    "report.aeroo.report": "report.report_aeroo.abstract",
    # ── A real model of another version or of a module this database does not
    # have, with a neighbour that answers the same question.
    "hr.contract": "hr.contract.type",
    "hr.expense.sheet": "hr.expense",
    "stock.valuation.layer": {"stock.move.valuation", "stock.move.valuation.line"},
    "sale.subscription": {"sale.subscription.plan", "sale.subscription.report"},
    "product.replenishment.cost": "product.replenish",
    "account.payment.group": {"account.payment", "account.group"},
    "adhocway.k8s.environment": "saas.environment",
    "adhocway.project": "project.project",
    # ── Nothing to offer. Saying so is the answer.
    "pos.order": None,
    "queue.job": None,
    "ir.property": None,
    "product.product_catalog_report": None,
    "school.school": None,
    "alerta": None,
    "runbot.branch": None,
    "runbot.build": None,
}

# The 12 the ranker gets wrong over the real registry — 49/61. Listed so a
# regression elsewhere cannot hide inside the pass rate: shrinking this set is
# the point, growing it fails the suite. Every repeated family passes; these are
# all singletons in the corpus.
#
# Two failure shapes, and the second is the open problem:
#
#   Answers instead of staying silent — `pos.order` → `sale.order`,
#   `queue.job` → `voip.queue.mixin`, `ir.property` → `ir.exports`,
#   `product.product_catalog_report` → `product.catalog.mixin`. Each shares one
#   word that clears the floor, and nothing says "different thing entirely".
#
#   ONE RARE WORD OUTVOTES SEVERAL COMMON ONES. `hr.expense.sheet` matches two
#   of three words in `hr.expense`, but `sheet` is rare enough that
#   `account.statement.import.sheet.parser` — one word of three — scores higher.
#   Same for `product.replenishment.cost` → `stock.replenishment.option`,
#   `project.main.database` → `mail.thread.main.attachment`,
#   `account.payment.group` → `account.asset.group`. Weighing coverage was tried
#   (`sum*cov`, `sum*cov²`, `sum+k*cov`, several floors) and none beat 49;
#   ranking by the shared FIRST word was tried and cost 10 cases, because
#   `adhoc.pull` → `saas.pull.request` shares no prefix at all.
#
#   `crm.lead.tag` → `crm.lead` and `helpdesk.ticket.tag` → `helpdesk.ticket`
#   are the same thing seen from the other side: `tag` is the word that names
#   what the caller wanted, and it is too common to win.
KNOWN_MISSES = frozenset(
    {
        # answers where silence was right
        "pos.order",
        "queue.job",
        "ir.property",
        "product.product_catalog_report",
        # one rare word beat several matching ones
        "hr.expense.sheet",
        "product.replenishment.cost",
        "project.main.database",
        "account.payment.group",
        "saas.database.module",
        "adhoc.module.version",
        # the naming word was too common to win
        "crm.lead.tag",
        "helpdesk.ticket.tag",
    }
)


def verdict(guess, suggestions):
    """True when ``suggestions`` answers ``guess`` acceptably."""
    expected = GUESSES[guess]
    head = suggestions[0] if suggestions else None
    if expected is None:
        return head is None
    if isinstance(expected, set):
        return head in expected
    return head == expected
