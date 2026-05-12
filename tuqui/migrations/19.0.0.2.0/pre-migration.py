"""Move v1 columns of tuqui.access.log to their v2 names before the new schema lands.

Sprint 1.6a redesigned the audit log:

- ``operation``    -> ``method``         (renamed; preserves data)
- ``record_count`` -> ``result_count``   (renamed; preserves data)
- ``timestamp``    -> dropped;            falls back to ``create_date``

Renames (not drops + new columns) so the NOT NULL constraint on
``method`` doesn't fire against existing rows. Each step is guarded
with a DO block so the script is idempotent — running it twice on the
same DB is a no-op.

The new columns (``operation_type``, ``policy_allowed``,
``policy_denied_reason``, ``duration_ms``, plus the existing
``success`` / ``error_code``) are created normally by Odoo when the
module loads, with their defaults applied to existing rows.
"""


def migrate(cr, version):
    if not version:
        # Fresh install — the v1 columns never existed.
        return
    cr.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tuqui_access_log' AND column_name = 'operation'
            ) THEN
                ALTER TABLE tuqui_access_log RENAME COLUMN operation TO method;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tuqui_access_log' AND column_name = 'record_count'
            ) THEN
                ALTER TABLE tuqui_access_log RENAME COLUMN record_count TO result_count;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tuqui_access_log' AND column_name = 'timestamp'
            ) THEN
                ALTER TABLE tuqui_access_log DROP COLUMN "timestamp";
            END IF;
        END $$;
        """
    )
