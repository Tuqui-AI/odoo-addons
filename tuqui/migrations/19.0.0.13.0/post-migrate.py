def migrate(cr, version):
    if not version:
        # Fresh install: default=True applies, existing clients start read-only.
        return
    # Upgrade path: existing OAuth clients keep write access (prior behaviour).
    cr.execute("UPDATE tuqui_oauth_client SET read_only = false")
