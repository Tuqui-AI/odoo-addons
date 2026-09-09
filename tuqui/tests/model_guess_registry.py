"""The whole model registry of one real Odoo, for calibrating the suggester.

Generated from adhoc.inc (Odoo 19, 1368 models) with:

    odoo_search_read('ir.model', [], fields=['model'], order='model asc')

Kept WHOLE on purpose. An earlier version of this fixture was 279 models
hand-picked as "the ones that could compete", which quietly broke the very
thing it was measuring: word weights are ``1.0 / occurrences``, so trimming the
registry inflates every weight — about 5x here — while ``_MODEL_MIN_SEGMENTS``
is an absolute floor. The corpus scored 54/61 on the trimmed list and 49/61 on
the real one, and the difference was not extra silence but confident wrong
answers. Whatever replaces this file has to be a complete registry too.

Not a snapshot to keep in sync: it is a fixed reference point. Regenerate only
to calibrate against a different Odoo, and re-run the corpus when you do.
"""

MODELS = """
_unknown
account.account account.account.fiscal.rate account.account.tag
account.accrued.orders.wizard account.aged.partner.balance.report.handler
account.aged.payable.report.handler account.aged.receivable.report.handler
account.analytic.account account.analytic.applicability
account.analytic.distribution.model account.analytic.line
account.analytic.line.calendar.employee account.analytic.plan account.ar.vat.line
account.asset account.asset.group account.asset.report.handler
account.audit.account.status account.auto.reconcile.wizard
account.automatic.entry.wizard account.autopost.bills.wizard
account.balance.sheet.report.handler account.balance_import_wizard
account.bank.reconciliation.report.handler account.bank.selection
account.bank.statement account.bank.statement.line
account.bank.statement.line.transient account.batch.error.wizard
account.batch.error.wizard.line account.batch.payment
account.cash.flow.report.handler account.cash.rounding account.change.company
account.change.currency account.change.lock.date account.chart.template
account.check.action.wizard account.check.reject.wizard
account.check.to_date.report.wizard account.code.mapping
account.create.batch.error.wizard account.customer.statement.report.handler
account.debit.note account.deferred.expense.report.handler
account.deferred.report.handler account.deferred.revenue.report.handler
account.direct_debit.mandate account.document.import.mixin
account.duplicate.transaction.wizard account.ec.sales.report.handler
account.edi.common account.edi.document account.edi.format account.edi.ubl
account.edi.xml.cii account.edi.xml.ubl_20 account.edi.xml.ubl_21
account.edi.xml.ubl_a_nz account.edi.xml.ubl_bis3 account.edi.xml.ubl_de
account.edi.xml.ubl_efff account.edi.xml.ubl_nl account.edi.xml.ubl_sg
account.financial.year.op account.fiscal.category account.fiscal.position
account.fiscal.position.account account.fiscal.position.l10n_ar_tax
account.fiscal.report.handler account.fiscal.year account.followup.report
account.followup.report.handler account.full.reconcile
account.general.ledger.report.handler account.generic.tax.report.handler
account.generic.tax.report.handler.account.tax
account.generic.tax.report.handler.tax.account account.group
account.import.summary account.incoterms account.invoice.report
account.invoice.tax account.invoice.tax_line account.journal
account.journal.book.group account.journal.book.report account.journal.group
account.journal.report.handler account.loan account.loan.close.wizard
account.loan.compute.wizard account.loan.line account.lock_exception
account.merge.wizard account.merge.wizard.line account.missing.transaction.wizard
account.move account.move.line account.move.reversal account.move.send
account.move.send.batch.wizard account.move.send.wizard
account.multicurrency.revaluation.report.handler
account.multicurrency.revaluation.wizard account.online.account
account.online.link account.partial.reconcile
account.partner.ledger.report.handler account.payment
account.payment.invoice.wizard account.payment.method
account.payment.method.line account.payment.receiptbook account.payment.register
account.payment.term account.payment.term.line account.reconcile.model
account.reconcile.model.line account.reconcile.wizard account.report
account.report.annotation account.report.budget account.report.budget.item
account.report.column account.report.custom.handler account.report.expression
account.report.external.value account.report.file.download.error.wizard
account.report.horizontal.group account.report.horizontal.group.rule
account.report.line account.report.send account.resequence.wizard account.return
account.return.check account.return.check.template account.return.creation.wizard
account.return.payment.wizard account.return.submission.wizard account.return.type
account.root account.secure.entries.wizard account.setup.bank.manual.config
account.statement.import account.statement.import.sheet.mapping
account.statement.import.sheet.parser account.tax account.tax.group
account.tax.repartition.line account.tax.report.handler
account.tax.settlement.wizard account.tax.unit account.transfer.model
account.transfer.model.line account.trial.balance.report.handler
account.write_off.type account_followup.followup.line
account_followup.manual_reminder account_followup.missing.information.wizard
account_reports.export.wizard account_reports.export.wizard.format
add.iot.box adhoc.module adhoc.module.category adhoc.module.create.task.wizard
adhoc.module.dependency adhoc.module.module adhoc.module.ready_to_upgrade.wizard
adhoc.module.repository adhoc.module.state adhoc.module.update.cloc.info
adhoc.product afip.activity afip.concept afip.import.wizard afip.import.wizard.line
afip.tax ai.agent ai.agent.instruction ai.agent.source ai.agent.source.rule
ai.composer ai.embedding ai.maturity.mixin ai.model.template
ai.model.template.preview.wizard ai.prompt.button ai.sub.agent ai.token.data.mixin
ai.tool.call.log ai.topic ai.video ai_documents.sort allow.billable.wizard
analytic.mixin analytic.plan.fields.mixin applicant.get.refuse.reason
applicant.send.mail appointment.answer appointment.answer.input
appointment.booking.line appointment.invite appointment.manage.leaves
appointment.question appointment.resource appointment.slot appointment.type
appraisal.ask.feedback appraisal.select.survey approval.approver approval.category
approval.category.approver approval.product.line approval.request arba.cot.wizard
asset.modify audit.report auth.oauth.provider auth.passkey.key
auth.passkey.key.create auth.totp.rate.limit.log auth_totp.device auth_totp.wizard
avatar.mixin barcode.nomenclature barcode.rule barcodes.barcode_events_mixin
base base.automation base.bg base.company.dependent base.document.layout
base.enable.profiling.wizard base.exception base.exception.method
base.geo_provider base.geocoder base.import.module base.language.export
base.language.import base.language.install base.module.install
base.module.install.request base.module.install.review base.module.uninstall
base.module.update base.module.upgrade base.partner.merge.automatic.wizard
base.partner.merge.line base_import.import base_import.mapping bg.job
bi.sql.view bi.sql.view.field bill.to.po.wizard blog.blog blog.post blog.tag
blog.tag.category board.board budget.analytic budget.line budget.report
budget.split.wizard bus.bus bus.listener.mixin calendar.alarm
calendar.alarm_manager calendar.attendee calendar.booking calendar.booking.line
calendar.event calendar.event.type calendar.filters
calendar.popover.delete.wizard calendar.provider.config calendar.recurrence
certificate.certificate certificate.key change.password.own change.password.user
change.password.wizard chatbot.message chatbot.script chatbot.script.answer
chatbot.script.step choose.delivery.carrier cleanup.create_indexes.line
cleanup.create_indexes.wizard cleanup.purge.line cleanup.purge.line.column
cleanup.purge.line.data cleanup.purge.line.field cleanup.purge.line.menu
cleanup.purge.line.model cleanup.purge.line.module cleanup.purge.line.table
cleanup.purge.wizard cleanup.purge.wizard.column cleanup.purge.wizard.data
cleanup.purge.wizard.field cleanup.purge.wizard.menu cleanup.purge.wizard.model
cleanup.purge.wizard.module cleanup.purge.wizard.table cnpg.settings.wizard
code.help.wizard confirm.stock.sms coupon.share crm.activity.report
crm.iap.lead.helpers crm.iap.lead.industry crm.iap.lead.mining.request
crm.iap.lead.role crm.iap.lead.seniority crm.lead crm.lead.assignation
crm.lead.convert2ticket crm.lead.forward.to.partner crm.lead.lost
crm.lead.pls.update crm.lead.scoring.frequency crm.lead.scoring.frequency.field
crm.lead2opportunity.partner crm.lead2opportunity.partner.mass crm.lost.reason
crm.merge.opportunity crm.partner.report.assign crm.quotation.partner
crm.recurring.plan crm.stage crm.tag crm.team crm.team.member
currency.rate.wizard customer.note.preview.wizard data_cleaning.model
data_cleaning.record data_cleaning.rule data_merge.group data_merge.model
data_merge.record data_merge.rule data_recycle.model data_recycle.record
decimal.precision delivery.carrier delivery.price.rule delivery.zip.prefix
dev.access.wizard digest.digest digest.tip discuss.call.history discuss.channel
discuss.channel.member discuss.channel.rtc.session discuss.gif.favorite
discuss.voice.metadata docs_config.installer documents.access
documents.access.tracking documents.account.folder.setting documents.document
documents.link_to_record_wizard documents.mixin documents.operation
documents.redirect documents.request_wizard documents.sharing
documents.sharing.access documents.tag documents.unlink.mixin
edit.billable.time.target electronic.payment.pending.confirm event.event
event.event.configurator event.event.ticket event.lead.request event.lead.rule
event.mail event.mail.registration event.mail.slot event.question
event.question.answer event.quiz event.quiz.answer event.quiz.question
event.registration event.registration.answer event.sale.report event.slot
event.stage event.tag event.tag.category event.track event.track.location
event.track.stage event.track.tag event.track.tag.category event.track.visitor
event.type event.type.mail event.type.ticket exception.rule
exception.rule.confirm expense.sample.receipt extract.mixin
extract.mixin.with.words fetchmail.server format.address.mixin
format.vat.label.mixin fsm.stock.tracking fsm.stock.tracking.line
gamification.badge gamification.badge.user gamification.badge.user.wizard
gamification.challenge gamification.challenge.line gamification.goal
gamification.goal.definition gamification.goal.wizard gamification.karma.rank
gamification.karma.tracking github.rate_limit google.calendar.account.reset
google.calendar.sync google.gmail.mixin google.service helm.app.version
helm.app.version.value helpdesk.create.fsm.task helpdesk.sla
helpdesk.sla.report.analysis helpdesk.sla.status helpdesk.solution
helpdesk.solution.faq.category helpdesk.solution.tag helpdesk.stage
helpdesk.stage.delete.wizard helpdesk.tag helpdesk.tag.assignment helpdesk.team
helpdesk.ticket helpdesk.ticket.convert.wizard helpdesk.ticket.customer_note
helpdesk.ticket.report.analysis helpdesk.ticket.to.lead
helpdesk.ticket.upgrade.request homework.location.wizard hr.applicant
hr.applicant.category hr.applicant.refuse.reason hr.applicant.skill hr.appraisal
hr.appraisal.campaign.wizard hr.appraisal.goal hr.appraisal.goal.skill
hr.appraisal.goal.tag hr.appraisal.note hr.appraisal.skill
hr.appraisal.skill.report hr.appraisal.template hr.attendance
hr.attendance.overtime.line hr.attendance.overtime.rule
hr.attendance.overtime.ruleset hr.bank.account.allocation.wizard
hr.bank.account.allocation.wizard.line hr.beneficios
hr.contract.recruitment.report hr.contract.salary.benefit
hr.contract.salary.benefit.type hr.contract.salary.benefit.value
hr.contract.salary.offer hr.contract.salary.offer.refusal.reason
hr.contract.salary.personal.info hr.contract.salary.personal.info.type
hr.contract.salary.personal.info.value hr.contract.salary.resume
hr.contract.salary.resume.category hr.contract.sign.document.wizard
hr.contract.signatory hr.contract.type hr.department hr.departure.reason
hr.departure.wizard hr.employee hr.employee.category
hr.employee.certification.report hr.employee.cv.wizard hr.employee.delete.wizard
hr.employee.location hr.employee.public hr.employee.skill
hr.employee.skill.history.report hr.employee.skill.report hr.expense
hr.expense.approve.duplicate hr.expense.post.wizard hr.expense.refuse.wizard
hr.expense.split hr.expense.split.wizard hr.holidays.cancel.leave
hr.holidays.summary.employee hr.individual.skill.mixin hr.job hr.job.platform
hr.job.seniority hr.job.skill hr.job.stage.rotting hr.leave
hr.leave.accrual.level hr.leave.accrual.plan hr.leave.allocation
hr.leave.allocation.generate.multi.wizard hr.leave.attendance.report
hr.leave.employee.type.report hr.leave.generate.multi.wizard
hr.leave.mandatory.day hr.leave.report hr.leave.report.calendar hr.leave.type
hr.manager.department.report hr.mixin hr.payroll.dashboard.warning
hr.payroll.declaration.mixin hr.payroll.edit.payslip.line
hr.payroll.edit.payslip.lines.wizard hr.payroll.edit.payslip.worked.days.line
hr.payroll.employee.declaration hr.payroll.headcount hr.payroll.headcount.line
hr.payroll.headcount.working.rate hr.payroll.index hr.payroll.note
hr.payroll.payment.report.wizard hr.payroll.structure hr.payroll.structure.type
hr.payslip hr.payslip.correction.wizard hr.payslip.input hr.payslip.input.type
hr.payslip.line hr.payslip.run hr.payslip.worked_days hr.recruitment.degree
hr.recruitment.report hr.recruitment.sign.document.wizard hr.recruitment.source
hr.recruitment.stage hr.recruitment.stage.report hr.referral.alert
hr.referral.alert.mail.wizard hr.referral.campaign.wizard hr.referral.friend
hr.referral.level hr.referral.link.to.share hr.referral.onboarding
hr.referral.points hr.referral.report hr.referral.reward
hr.referral.reward.report hr.referral.send.mail hr.referral.send.sms
hr.resume.line hr.resume.line.type hr.rule.parameter hr.rule.parameter.value
hr.salary.attachment hr.salary.rule hr.salary.rule.category
hr.salary.rule.section hr.salary_category hr.seniority hr.skill hr.skill.level
hr.skill.type hr.talent.pool hr.timesheet.attendance.report
hr.timesheet.stop.timer.confirmation.wizard hr.timesheet.tip
hr.user.work.entry.employee hr.version hr.version.wizard hr.work.entry
hr.work.entry.export.employee.mixin hr.work.entry.export.mixin
hr.work.entry.regeneration.wizard hr.work.entry.report hr.work.entry.type
hr.work.location hr_employee.beneficio hr_timesheet.merge.wizard
html.field.history.mixin html_editor.converter.test html_editor.converter.test.sub
iap.account iap.autocomplete.api iap.enrich.api iap.service im_livechat.channel
im_livechat.channel.member.history im_livechat.channel.rule
im_livechat.conversation.tag im_livechat.expertise im_livechat.report.channel
image.mixin inflation.adjustment inflation.adjustment.index iot.box iot.channel
iot.device iot.discovered.box iot.keyboard.layout ir.actions.act_url
ir.actions.act_window ir.actions.act_window.view ir.actions.act_window_close
ir.actions.actions ir.actions.client ir.actions.report ir.actions.server
ir.actions.server.history ir.actions.server.mass.edit.line ir.actions.todo
ir.asset ir.attachment ir.attachment.report ir.autovacuum ir.binary
ir.config_parameter ir.cron ir.cron.progress ir.cron.trigger ir.default ir.demo
ir.demo_failure ir.demo_failure.wizard ir.embedded.actions ir.exports
ir.exports.line ir.fields.converter ir.filters ir.http ir.logging ir.mail_server
ir.model ir.model.access ir.model.constraint ir.model.data
ir.model.dynamic_message ir.model.dynamic_message.line ir.model.fields
ir.model.fields.selection ir.model.inherit ir.model.relation ir.module.category
ir.module.module ir.module.module.dependency ir.module.module.exclusion
ir.profile ir.qweb ir.qweb.field ir.qweb.field.barcode ir.qweb.field.contact
ir.qweb.field.date ir.qweb.field.datetime ir.qweb.field.duration
ir.qweb.field.float ir.qweb.field.float_time ir.qweb.field.html
ir.qweb.field.image ir.qweb.field.image_url ir.qweb.field.integer
ir.qweb.field.many2many ir.qweb.field.many2one ir.qweb.field.monetary
ir.qweb.field.one2many ir.qweb.field.qweb ir.qweb.field.relative
ir.qweb.field.selection ir.qweb.field.text ir.qweb.field.time ir.rule ir.sequence
ir.sequence.date_range ir.ui.menu ir.ui.view ir.ui.view.custom ir.websocket
ir_actions_account_report_download job.add.applicants knowledge.article
knowledge.article.favorite knowledge.article.member knowledge.article.stage
knowledge.article.template.category knowledge.article.thread knowledge.cover
knowledge.invite kpi.provider l10n_ar.afip.responsibility.type
l10n_ar.afipws.connection l10n_ar.arca.activity l10n_ar.arca.connection.wizard
l10n_ar.arca.journal.wizard l10n_ar.arca.journal.wizard.line
l10n_ar.boarding_permission l10n_ar.caba.report.handler l10n_ar.earnings.scale
l10n_ar.earnings.scale.line l10n_ar.iva.report.handler
l10n_ar.mendoza.report.handler l10n_ar.misiones.report.handler
l10n_ar.partner.tax l10n_ar.payment.register.withholding
l10n_ar.payment.withholding l10n_ar.pba.report.handler
l10n_ar.santa_fe.report.handler l10n_ar.sicore.report.handler
l10n_ar.sifere.report.handler l10n_ar.sircar.report.handler
l10n_ar.tax.report.handler l10n_ar.tucuman.report.handler l10n_ar_afip.ws.consult
l10n_cl.company.activities l10n_cl.dte.caf l10n_cl.edi.reference
l10n_cl.edi.util l10n_cl.report.handler l10n_cl.tax.report.handler
l10n_cl_reports_f29.return.submission.wizard l10n_latam.check
l10n_latam.document.type l10n_latam.identification.type
l10n_latam.payment.mass.transfer l10n_latam.payment.register.check
l10n_us.1099_box l10n_us.tax.report.handler l10n_us_1099.wizard link.tracker
link.tracker.click link.tracker.code lot.label.layout loyalty.card
loyalty.card.update.balance loyalty.generate.wizard loyalty.history
loyalty.mail loyalty.program loyalty.reward loyalty.rule mail.activity
mail.activity.mixin mail.activity.plan mail.activity.plan.template
mail.activity.schedule mail.activity.schedule.line mail.activity.todo.create
mail.activity.type mail.alias mail.alias.domain mail.alias.mixin
mail.alias.mixin.optional mail.blacklist mail.blacklist.remove mail.bot
mail.canned.response mail.compose.message mail.composer.mixin mail.followers
mail.followers.edit mail.gateway.allowed mail.group mail.group.member
mail.group.message mail.group.message.reject mail.group.moderation mail.guest
mail.ice.server mail.link.preview mail.mail mail.message
mail.message.link.preview mail.message.reaction mail.message.schedule
mail.message.subtype mail.message.translation mail.notification mail.presence
mail.push mail.push.device mail.render.mixin mail.scheduled.message
mail.server.test.wizard mail.template mail.template.preview mail.template.reset
mail.thread mail.thread.blacklist mail.thread.cc mail.thread.main.attachment
mail.thread.phone mail.tracking.duration.mixin mail.tracking.value
mailing.contact mailing.contact.import mailing.contact.to.list mailing.filter
mailing.list mailing.list.merge mailing.mailing mailing.mailing.schedule.date
mailing.mailing.test mailing.subscription mailing.subscription.optout
mailing.trace mailing.trace.report maintenance.equipment
maintenance.equipment.category maintenance.mixin maintenance.request
maintenance.stage maintenance.team major.version.change marketing.activity
marketing.campaign marketing.campaign.test marketing.participant
marketing.trace mass.editing.wizard meeting.notes microsoft.outlook.mixin
model.classifier model.classifier.tag model.classifier.version
oauth.provider.authorization.code oauth.provider.client
oauth.provider.redirect.uri oauth.provider.scope oauth.provider.token
onboarding.onboarding onboarding.onboarding.step onboarding.progress
onboarding.progress.step pagos360.card.brand pagos360.channel
pagos360.installment payment.capture.wizard payment.link.wizard payment.method
payment.provider payment.provider.log payment.refund.wizard payment.token
payment.transaction payment.transaction.retry payment.transaction.retry.lines
phone.blacklist phone.blacklist.remove picking.label.type
planning.analysis.report planning.attendance.analysis.report
planning.calendar.resource planning.planning planning.preview planning.recurrency
planning.role planning.send planning.slot planning.slot.template portal.mixin
portal.sections.wizard portal.share portal.wizard portal.wizard.user privacy.log
privacy.lookup.wizard privacy.lookup.wizard.line product.attribute
product.attribute.category product.attribute.custom.value
product.attribute.value product.catalog.mixin product.category product.combo
product.combo.item product.document product.feed product.fetch.image.wizard
product.image product.label.layout product.pack.line product.pricelist
product.pricelist.item product.product product.public.category product.removal
product.replenish product.ribbon product.supplierinfo product.tag
product.template product.template.attribute.exclusion
product.template.attribute.line product.template.attribute.value product.uom
product.value product.wishlist project.collaborator project.complexity
project.log project.milestone project.project project.project.stage
project.project.stage.delete.wizard project.role
project.sale.line.employee.map project.share.collaborator.wizard
project.share.wizard project.tags project.task
project.task.burndown.chart.report project.task.convert.wizard
project.task.recurrence project.task.stage.personal
project.task.stop.timers.wizard project.task.stop.timers.wizard.line
project.task.type project.task.type.delete.wizard project.template.create.wizard
project.template.role.to.users.map project.timesheet.forecast.report.analysis
project.update project.update.type properties.base.definition
properties.base.definition.mixin publisher_warranty.contract
purchase.bill.line.match purchase.bill.union purchase.change.currency
purchase.edi.xml.ubl_bis3 purchase.order purchase.order.cancel.remaining
purchase.order.global_discount.wizard purchase.order.line
purchase.order.line.add_to_invoice purchase.report purchase.request
purchase.request.allocation purchase.request.line
purchase.request.line.make.purchase.order
purchase.request.line.make.purchase.order.item purchase.subscription
purchase.subscription.close.reason purchase.subscription.line
qr.code.payment.wizard quotation.document rating.category rating.mixin
rating.parent.mixin rating.rating refuse.offer.wizard registration.editor
registration.editor.line report.account.report_hash_integrity
report.account.report_invoice report.account.report_invoice_with_payments
report.account_batch_payment.print_batch_payment
report.base.report_irmodulereference
report.event_iot.event_registration_badge_printer_report
report.hr_holidays.report_holidayssummary
report.hr_payroll.contribution_register report.hr_skills.report_employee_cv
report.industry_fsm.worksheet_custom report.layout report.mimetypes
report.paperformat report.product.report_pricelist
report.product.report_producttemplatelabel2x7
report.product.report_producttemplatelabel4x12
report.product.report_producttemplatelabel4x12noprice
report.product.report_producttemplatelabel4x7
report.product.report_producttemplatelabel_dymo report.product_template_printer
report.project.task.user report.project.task.user.fsm
report.report_aeroo.abstract report.report_xlsx.abstract
report.report_xlsx.partner_xlsx report.sample_report
report.sign.green_savings_report report.stock.label_lot_template_view
report.stock.label_product_product_view report.stock.quantity
report.stock.report_reception report.stock.report_stock_rule report.stylesheets
request.appraisal res.bank res.city res.company res.company.jurisdiction.padron
res.config res.config.settings res.country res.country.group res.country.state
res.currency res.currency.rate res.device res.device.log res.groups
res.groups.privilege res.lang res.partner res.partner.activation
res.partner.bank res.partner.category res.partner.grade res.partner.industry
res.partner.tag res.partner.update.from.padron.field
res.partner.update.from.padron.info res.partner.update.from.padron.wizard
res.role res.users res.users.apikeys res.users.apikeys.description
res.users.apikeys.show res.users.deletion res.users.identitycheck res.users.log
res.users.settings res.users.settings.embedded.action res.users.settings.volumes
reset.view.arch.wizard resource.calendar resource.calendar.attendance
resource.calendar.leaves resource.mixin resource.resource room.booking
room.office room.room saas.automatic.upgrade.request.wizard saas.backup_list
saas.backup_list.line saas.bucket saas.bucket.config saas.bypass_protection
saas.choose_database_type saas.cluster saas.cnpg.snapshot saas.communication
saas.communication.category saas.database saas.database.access.request
saas.database.custom_domain saas.database.egress.domain saas.database.kpi
saas.database.kpi.category saas.database.kpi.line saas.database.task
saas.database.type saas.database.user_list saas.database.user_list.user
saas.database.wizard saas.enterprise.subscription saas.environment
saas.environment.postgres.server saas.introspection saas.mail.count
saas.neutralize.script saas.notification saas.odoo.major_version
saas.odoo.version saas.odoo.version.group saas.odoo.version.group.documentation
saas.odoo.version.repository saas.odoo.version.repository.commit
saas.padron.agip saas.padron.agip.line saas.partner.scope saas.portal.access
saas.postgres.server saas.prometheus.collector
saas.provider.upgrade.revert.wizard saas.provider.upgrade.util saas.pull.request
saas.question saas.question.type saas.repositories.update
saas.upgrade.client.config.line saas.upgrade.client.data saas.upgrade.line
saas.upgrade.line.request.log saas.upgrade.line.request.log.entry
saas.upgrade.line.request.run saas.upgrade.line.script saas.upgrade.type
saas.upgrade.upload.changes saas_client.dashboard sale.advance.payment.inv
sale.edi.xml.ubl_bis3 sale.exception.confirm sale.loyalty.coupon.wizard
sale.loyalty.reward.wizard sale.mass.cancel.orders sale.order
sale.order.analytic.wizard sale.order.analytic.wizard.line
sale.order.cancel.remaining sale.order.close.reason sale.order.coupon.points
sale.order.discount sale.order.global_discount.wizard sale.order.line
sale.order.log sale.order.log.report sale.order.spreadsheet sale.order.template
sale.order.template.line sale.order.type sale.pdf.form.field sale.report
sale.subscription.change.customer.wizard sale.subscription.close.reason.wizard
sale.subscription.plan sale.subscription.price.update.wizard
sale.subscription.pricelist.display sale.subscription.report
save.spreadsheet.template select.printers.wizard sequence.mixin
server.action.history.wizard shared.to.branches.mixin sign.completed.document
sign.document sign.import.documents sign.item sign.item.option
sign.item.radio.set sign.item.role sign.item.type sign.log sign.request
sign.request.item sign.request.item.value sign.request.share sign.send.request
sign.send.request.signer sign.template sign.template.preview sign.template.tag
slide.answer slide.channel slide.channel.invite slide.channel.partner
slide.channel.tag slide.channel.tag.group slide.embed slide.question slide.slide
slide.slide.partner slide.slide.resource slide.tag sms.account.code
sms.account.phone sms.account.sender sms.composer sms.sms sms.template
sms.template.preview sms.template.reset sms.tracker snailmail.letter
social.account social.account.revoke.youtube social.live.post social.media
social.post social.post.template social.post.to.lead social.stream
social.stream.post social.stream.post.image social.stream.type
social.twitter.account spreadsheet.cell.thread spreadsheet.contributor
spreadsheet.dashboard spreadsheet.dashboard.group spreadsheet.dashboard.share
spreadsheet.document.to.dashboard spreadsheet.mixin spreadsheet.revision
spreadsheet.template sql.request.mixin stock.avco.report
stock.backorder.confirmation stock.backorder.confirmation.line
stock.forecasted_product_product stock.forecasted_product_template
stock.inventory.adjustment.name stock.inventory.conflict stock.inventory.warning
stock.location stock.lot stock.move stock.move.line stock.move.valuation
stock.move.valuation.line stock.operation.wizard stock.orderpoint.snooze
stock.package stock.package.destination stock.package.history stock.package.type
stock.picking stock.picking.type stock.picking.zpl.lines stock.product.zpl.lines
stock.put.in.pack stock.putaway.rule stock.quant stock.quant.relocate
stock.quantity.history stock.reference stock.replenish.mixin
stock.replenishment.info stock.replenishment.option stock.report
stock.request.count stock.return.picking stock.return.picking.line stock.route
stock.rule stock.rules.report stock.scrap stock.scrap.reason.tag
stock.storage.category stock.storage.category.capacity
stock.traceability.report stock.warehouse stock.warehouse.orderpoint
stock.warn.insufficient.qty stock.warn.insufficient.qty.scrap
stock_account.stock.valuation.report stock_barcode.cancel.operation
studio.approval.entry studio.approval.request studio.approval.rule
studio.approval.rule.approver studio.approval.rule.delegate studio.export.model
studio.export.wizard studio.export.wizard.data studio.mixin
subscription.billing.run subscription.billing.run.line survey.invite
survey.question survey.question.answer survey.survey survey.user_input
survey.user_input.line talent.pool.add.applicants task.share.wizard
template.reset.mixin theme.ir.asset theme.ir.attachment theme.ir.ui.view
theme.utils theme.website.menu theme.website.page timer.mixin timer.parent.mixin
timer.timer timesheet.grid.mixin timesheets.analysis.report tuqui.access.log
tuqui.activation.nonce tuqui.assistant.sso.nonce tuqui.event tuqui.oauth.client
uom.uom update.product.attribute.value upgrade.portal.step utm.campaign
utm.medium utm.mixin utm.source utm.source.mixin utm.stage utm.tag
validate.account.move value.proposition vendor.delay.report voip.call
voip.country.code.mixin voip.provider voip.queue.mixin
web.environment.ribbon.backend web_tour.tour web_tour.tour.step website
website.assets website.base.unit website.checkout.step
website.configurator.feature website.controller.page
website.cover_properties.mixin website.custom_blocked_third_party_domains
website.event.menu website.html.text.processor website.menu
website.multi.mixin website.page website.page.properties
website.page.properties.base website.page_options.mixin
website.page_visibility_options.mixin website.published.mixin
website.published.multi.mixin website.rewrite website.robots website.route
website.sale.extra.field website.searchable.mixin website.seo.metadata
website.snippet.filter website.technical.page website.track website.visitor
website.visitor.push.subscription whatsapp.account whatsapp.composer
whatsapp.conversation whatsapp.message whatsapp.partner.bsuid whatsapp.preview
whatsapp.register.phone whatsapp.template whatsapp.template.button
whatsapp.template.variable wizard.direct.debit.payment
wizard.ir.model.menu.create worksheet.template worksheet.template.load.wizard
x_bi_sql_view.actua_info_customers x_bi_sql_view.analisis_net_sale
x_bi_sql_view.cobrabilidad x_bi_sql_view.internal_users_messages
x_bi_sql_view.sla_mda x_bi_sql_view.tickets_por_usuario
x_bi_sql_view.upgrade_info_clientes x_bve.facturas x_bve.test x_bve.ventas
x_etiquetas x_hr.beneficios x_hr_employee.beneficio x_motivo_padre
x_project_task_worksheet_template_1 x_user_target
""".split()
