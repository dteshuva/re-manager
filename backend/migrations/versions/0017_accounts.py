"""multi-tenancy: accounts, and an account_id on every tenant-owned table

Why:
    Until now the deployment held ONE portfolio shared by every user: `properties`,
    `categories`, `attention_settings` and the portfolio rollup were global, and any
    logged-in user saw (and could edit) all of it. This migration makes an **account**
    the tenancy boundary: each account owns its own properties, categories, thresholds
    and rollups, and users belong to exactly one account.

Ownership model:
    ``accounts`` is the tenant. ``users.account_id`` says who you are acting for.
    ``properties.account_id`` is the root of the ownership tree — units, leases,
    budgets, tags, investments and period status hang off a property by FK CASCADE
    and are therefore scoped transitively (the app resolves a property by
    ``(id, account_id)`` before touching any of them).

    Five tables are NOT property-derived and so carry ``account_id`` in their own right:
    ``users``, ``categories``, ``attention_settings``, ``portfolio_month_summary`` and
    ``audit_log``.

    ``monthly_records``, ``line_items`` and the three summary tables carry a
    DENORMALIZED ``account_id``. For the summaries that is purely so a dashboard read is
    one indexed scan instead of a join through ``properties``. For the two financial
    tables it buys something stronger — see below.

DB-enforced cross-account invariant (the important part):
    A line item points at a category, and a category now belongs to an account. Nothing
    in the FK graph stopped account A's line item from referencing account B's category,
    which would let B's reclassification move A's NOI. Rather than rely on an app-level
    check, this migration closes it structurally, in the same style as the existing
    ``monthly_records_unit_in_property_fk``:

        monthly_records (account_id, property_id)      -> properties (account_id, id)
        line_items      (account_id, monthly_record_id) -> monthly_records (account_id, id)
        line_items      (account_id, category_id)       -> categories (account_id, id)

    Together these make a cross-account P&L row unrepresentable, not merely rejected.

Rollup functions and views:
    ``v_line_item_resolved`` / ``v_monthly_pnl`` / ``v_portfolio_monthly`` gain
    ``account_id`` (functionally dependent on property_id, so it just rides along in the
    GROUP BY). ``refresh_portfolio_month_summary`` CHANGES SIGNATURE — it used to sum
    every property in a month, which is exactly the leak this migration exists to close;
    it is now ``(p_account_id, p_month)``. ``refresh_month_summaries`` resolves the
    account from the property, so its callers in app/summaries.py are unchanged.

Existing data:
    All current rows are adopted by a single account named 'Default Portfolio'.

Revision ID: 0017_accounts
Revises: 0016_lease_concession
Create Date: 2026-07-22

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0017_accounts"
down_revision: Union[str, None] = "0016_lease_concession"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_METRICS = (
    "gross_rent", "operating_expenses", "noi", "capex",
    "debt_service", "other_below_line", "below_noi", "cash_flow",
)

# Tables that get a plain account_id column backfilled from the default account.
# (property-derived ones are backfilled from their property instead — see upgrade()).
_SIMPLE_BACKFILL = ("users", "categories", "audit_log")

_PROPERTY_DERIVED = (
    "monthly_records",
    "property_month_summary",
    "unit_month_summary",
    "property_category_month_summary",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    #  1. The tenant table, plus the account that adopts all existing data.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE accounts (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT accounts_name_nonempty CHECK (length(trim(name)) > 0)
        );
        """
    )
    op.execute("INSERT INTO accounts (name) VALUES ('Default Portfolio');")

    # ------------------------------------------------------------------
    #  2. account_id everywhere, backfilled to the default account.
    # ------------------------------------------------------------------
    for table in _SIMPLE_BACKFILL:
        op.execute(f"ALTER TABLE {table} ADD COLUMN account_id UUID;")
        op.execute(f"UPDATE {table} SET account_id = (SELECT id FROM accounts LIMIT 1);")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN account_id SET NOT NULL;")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_account_fk "
            "FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE;"
        )
        op.execute(f"CREATE INDEX {table}_account_idx ON {table} (account_id);")

    # properties is the root of the ownership tree, so it is backfilled first and the
    # property-derived tables then read their account_id back off it.
    op.execute("ALTER TABLE properties ADD COLUMN account_id UUID;")
    op.execute("UPDATE properties SET account_id = (SELECT id FROM accounts LIMIT 1);")
    op.execute("ALTER TABLE properties ALTER COLUMN account_id SET NOT NULL;")
    op.execute(
        "ALTER TABLE properties ADD CONSTRAINT properties_account_fk "
        "FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE;"
    )
    op.execute("CREATE INDEX properties_account_idx ON properties (account_id);")
    # FK target for the composite (account_id, property_id) FKs below.
    op.execute(
        "ALTER TABLE properties ADD CONSTRAINT properties_account_id_id_key "
        "UNIQUE (account_id, id);"
    )

    for table in _PROPERTY_DERIVED:
        op.execute(f"ALTER TABLE {table} ADD COLUMN account_id UUID;")
        op.execute(
            f"UPDATE {table} t SET account_id = p.account_id "
            "FROM properties p WHERE p.id = t.property_id;"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN account_id SET NOT NULL;")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_account_fk "
            "FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE;"
        )

    # The dashboards' hot paths: "this account's rows for this month".
    op.execute(
        "CREATE INDEX property_month_summary_account_month_idx "
        "ON property_month_summary (account_id, month);"
    )
    op.execute(
        "CREATE INDEX unit_month_summary_account_month_idx "
        "ON unit_month_summary (account_id, month);"
    )
    op.execute(
        "CREATE INDEX pcms_account_month_class_idx "
        "ON property_category_month_summary (account_id, month, classification);"
    )
    op.execute(
        "CREATE INDEX monthly_records_account_month_idx "
        "ON monthly_records (account_id, month);"
    )

    # ------------------------------------------------------------------
    #  3. Per-account uniqueness where the old constraint was global.
    # ------------------------------------------------------------------
    # Two accounts must both be able to have a category called "Rent".
    op.execute("ALTER TABLE categories DROP CONSTRAINT categories_name_key;")
    op.execute(
        "ALTER TABLE categories ADD CONSTRAINT categories_account_name_key "
        "UNIQUE (account_id, name);"
    )
    op.execute(
        "ALTER TABLE categories ADD CONSTRAINT categories_account_id_id_key "
        "UNIQUE (account_id, id);"
    )

    # ------------------------------------------------------------------
    #  4. The composite FKs that make a cross-account P&L row unrepresentable.
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE monthly_records ADD CONSTRAINT monthly_records_property_in_account_fk "
        "FOREIGN KEY (account_id, property_id) REFERENCES properties (account_id, id) "
        "ON DELETE CASCADE;"
    )
    op.execute(
        "ALTER TABLE monthly_records ADD CONSTRAINT monthly_records_account_id_id_key "
        "UNIQUE (account_id, id);"
    )

    op.execute("ALTER TABLE line_items ADD COLUMN account_id UUID;")
    op.execute(
        "UPDATE line_items li SET account_id = mr.account_id "
        "FROM monthly_records mr WHERE mr.id = li.monthly_record_id;"
    )
    op.execute("ALTER TABLE line_items ALTER COLUMN account_id SET NOT NULL;")
    op.execute(
        "ALTER TABLE line_items ADD CONSTRAINT line_items_record_in_account_fk "
        "FOREIGN KEY (account_id, monthly_record_id) "
        "REFERENCES monthly_records (account_id, id) ON DELETE CASCADE;"
    )
    op.execute(
        "ALTER TABLE line_items ADD CONSTRAINT line_items_category_in_account_fk "
        "FOREIGN KEY (account_id, category_id) REFERENCES categories (account_id, id);"
    )

    # ------------------------------------------------------------------
    #  5. attention_settings: one pinned row -> one row per account.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE attention_settings ADD COLUMN account_id UUID;")
    op.execute("UPDATE attention_settings SET account_id = (SELECT id FROM accounts LIMIT 1);")
    op.execute("ALTER TABLE attention_settings DROP CONSTRAINT attention_settings_pkey;")
    op.execute("ALTER TABLE attention_settings DROP COLUMN id;")
    op.execute("ALTER TABLE attention_settings ALTER COLUMN account_id SET NOT NULL;")
    op.execute("ALTER TABLE attention_settings ADD PRIMARY KEY (account_id);")
    op.execute(
        "ALTER TABLE attention_settings ADD CONSTRAINT attention_settings_account_fk "
        "FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE;"
    )

    # ------------------------------------------------------------------
    #  6. portfolio_month_summary: one row per month -> per (account, month).
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE portfolio_month_summary ADD COLUMN account_id UUID;")
    op.execute(
        "UPDATE portfolio_month_summary SET account_id = (SELECT id FROM accounts LIMIT 1);"
    )
    op.execute("ALTER TABLE portfolio_month_summary ALTER COLUMN account_id SET NOT NULL;")
    op.execute("ALTER TABLE portfolio_month_summary DROP CONSTRAINT portfolio_month_summary_pkey;")
    op.execute("ALTER TABLE portfolio_month_summary ADD PRIMARY KEY (account_id, month);")
    op.execute(
        "ALTER TABLE portfolio_month_summary ADD CONSTRAINT portfolio_month_summary_account_fk "
        "FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE;"
    )

    # ------------------------------------------------------------------
    #  7. Views carry account_id.
    # ------------------------------------------------------------------
    _create_views(with_account=True)

    # ------------------------------------------------------------------
    #  8. Account-aware rollup functions.
    # ------------------------------------------------------------------
    # Drop the global (month-only) portfolio refresh outright rather than leave it
    # beside the new overload — an unscoped "sum every property this month" is the
    # exact leak this migration closes, and a stale caller must fail loudly.
    op.execute("DROP FUNCTION IF EXISTS refresh_portfolio_month_summary(DATE);")
    _create_functions(with_account=True)

    op.execute("SELECT rebuild_all_summaries();")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_portfolio_monthly;")
    op.execute("DROP VIEW IF EXISTS v_monthly_pnl;")
    op.execute("DROP VIEW IF EXISTS v_line_item_resolved;")

    op.execute("ALTER TABLE line_items DROP CONSTRAINT line_items_category_in_account_fk;")
    op.execute("ALTER TABLE line_items DROP CONSTRAINT line_items_record_in_account_fk;")
    op.execute("ALTER TABLE line_items DROP COLUMN account_id;")

    op.execute(
        "ALTER TABLE monthly_records DROP CONSTRAINT monthly_records_account_id_id_key;"
    )
    op.execute(
        "ALTER TABLE monthly_records DROP CONSTRAINT monthly_records_property_in_account_fk;"
    )

    op.execute("ALTER TABLE categories DROP CONSTRAINT categories_account_id_id_key;")
    op.execute("ALTER TABLE categories DROP CONSTRAINT categories_account_name_key;")
    op.execute("ALTER TABLE categories ADD CONSTRAINT categories_name_key UNIQUE (name);")

    op.execute("ALTER TABLE portfolio_month_summary DROP CONSTRAINT portfolio_month_summary_pkey;")
    op.execute("ALTER TABLE portfolio_month_summary DROP COLUMN account_id;")
    op.execute("ALTER TABLE portfolio_month_summary ADD PRIMARY KEY (month);")

    op.execute("ALTER TABLE attention_settings DROP CONSTRAINT attention_settings_pkey;")
    op.execute("ALTER TABLE attention_settings DROP COLUMN account_id;")
    op.execute("ALTER TABLE attention_settings ADD COLUMN id INTEGER NOT NULL DEFAULT 1;")
    op.execute("ALTER TABLE attention_settings ADD PRIMARY KEY (id);")
    op.execute(
        "ALTER TABLE attention_settings ADD CONSTRAINT attention_settings_single_row "
        "CHECK (id = 1);"
    )

    for table in _PROPERTY_DERIVED:
        op.execute(f"ALTER TABLE {table} DROP COLUMN account_id;")
    op.execute("ALTER TABLE properties DROP CONSTRAINT properties_account_id_id_key;")
    op.execute("ALTER TABLE properties DROP COLUMN account_id;")
    for table in _SIMPLE_BACKFILL:
        op.execute(f"ALTER TABLE {table} DROP COLUMN account_id;")

    op.execute("DROP TABLE accounts;")

    _create_views(with_account=False)
    op.execute("DROP FUNCTION IF EXISTS refresh_portfolio_month_summary(UUID, DATE);")
    _create_functions(with_account=False)
    op.execute("SELECT rebuild_all_summaries();")


# ----------------------------------------------------------------------
#  Shared DDL builders (upgrade passes with_account=True, downgrade False)
# ----------------------------------------------------------------------
def _create_views(*, with_account: bool) -> None:
    acct_col = "mr.account_id     AS account_id," if with_account else ""
    acct_sel = "account_id," if with_account else ""
    acct_grp = "account_id, " if with_account else ""

    op.execute("DROP VIEW IF EXISTS v_portfolio_monthly;")
    op.execute("DROP VIEW IF EXISTS v_monthly_pnl;")
    op.execute("DROP VIEW IF EXISTS v_line_item_resolved;")

    op.execute(
        f"""
        CREATE VIEW v_line_item_resolved AS
        SELECT
            li.id            AS line_item_id,
            li.amount        AS amount,
            COALESCE(li.classification, c.default_classification) AS classification,
            c.id             AS category_id,
            c.name           AS category_name,
            {acct_col}
            mr.property_id   AS property_id,
            mr.unit_id       AS unit_id,
            mr.month         AS month
        FROM line_items li
        JOIN categories      c  ON c.id  = li.category_id
        JOIN monthly_records mr ON mr.id = li.monthly_record_id;
        """
    )

    op.execute(
        f"""
        CREATE VIEW v_monthly_pnl AS
        SELECT
            {acct_sel}
            property_id,
            unit_id,
            month,
            COALESCE(SUM(amount) FILTER (WHERE classification = 'rent'), 0)      AS gross_rent,
            COALESCE(SUM(amount) FILTER (WHERE classification = 'operating'), 0) AS operating_expenses,
            COALESCE(SUM(amount) FILTER (WHERE classification = 'rent'), 0)
              - COALESCE(SUM(amount) FILTER (WHERE classification = 'operating'), 0) AS noi,
            COALESCE(SUM(amount) FILTER (WHERE classification = 'capex'), 0)        AS capex,
            COALESCE(SUM(amount) FILTER (WHERE classification = 'debt_service'), 0) AS debt_service,
            COALESCE(SUM(amount) FILTER (WHERE classification = 'other_below_line'), 0)
                                                                                   AS other_below_line,
            COALESCE(SUM(amount) FILTER (WHERE classification NOT IN ('rent', 'operating')), 0)
                                                                                   AS below_noi,
            (
                COALESCE(SUM(amount) FILTER (WHERE classification = 'rent'), 0)
                - COALESCE(SUM(amount) FILTER (WHERE classification = 'operating'), 0)
            )
            - COALESCE(SUM(amount) FILTER (WHERE classification NOT IN ('rent', 'operating')), 0)
                                                                                   AS cash_flow
        FROM v_line_item_resolved
        GROUP BY {acct_grp}property_id, unit_id, month;
        """
    )

    op.execute(
        f"""
        CREATE VIEW v_portfolio_monthly AS
        SELECT
            {acct_sel}
            month,
            SUM(gross_rent)        AS gross_rent,
            SUM(operating_expenses) AS operating_expenses,
            SUM(noi)               AS noi,
            SUM(capex)             AS capex,
            SUM(debt_service)      AS debt_service,
            SUM(other_below_line)  AS other_below_line,
            SUM(below_noi)         AS below_noi,
            SUM(cash_flow)         AS cash_flow
        FROM v_monthly_pnl
        GROUP BY {acct_grp}month
        ORDER BY {acct_grp}month;
        """
    )


def _create_functions(*, with_account: bool) -> None:
    """(Re)create the four refresh functions.

    with_account=True is the account-scoped form this migration introduces;
    False restores the pre-0017 global form for downgrade.
    """
    metrics_csv = ", ".join(_METRICS)
    sum_metrics = ",\n            ".join(f"COALESCE(SUM(v.{m}), 0)" for m in _METRICS)
    select_metrics = ",\n            ".join(f"COALESCE(v.{m}, 0)" for m in _METRICS)
    upsert_metrics = ",\n            ".join(f"{m} = EXCLUDED.{m}" for m in _METRICS)
    portfolio_sum = ",\n            ".join(f"COALESCE(SUM({m}), 0)" for m in _METRICS)

    # --- property-month -------------------------------------------------
    acct_decl = "v_account_id  UUID;" if with_account else ""
    acct_lookup = (
        "SELECT account_id INTO v_account_id FROM properties WHERE id = p_property_id;"
        if with_account
        else ""
    )
    acct_ins = "account_id, " if with_account else ""
    acct_val = "v_account_id, " if with_account else ""

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_property_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            v_total_units INTEGER;
            v_occupied    INTEGER;
            {acct_decl}
        BEGIN
            {acct_lookup}
            SELECT count(*) INTO v_total_units
            FROM units WHERE property_id = p_property_id;

            SELECT count(DISTINCT v.unit_id) INTO v_occupied
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id
              AND v.month = p_month
              AND v.unit_id IS NOT NULL
              AND v.gross_rent > 0;

            INSERT INTO property_month_summary AS s (
                {acct_ins}property_id, month, {metrics_csv},
                occupied_units, total_units, occupancy, refreshed_at
            )
            SELECT
                {acct_val}p_property_id, p_month,
                {sum_metrics},
                v_occupied,
                v_total_units,
                CASE WHEN v_total_units > 0
                     THEN ROUND(v_occupied::numeric / v_total_units, 5)
                     ELSE NULL END,
                now()
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id AND v.month = p_month
            ON CONFLICT (property_id, month) DO UPDATE SET
                {upsert_metrics},
                occupied_units = EXCLUDED.occupied_units,
                total_units    = EXCLUDED.total_units,
                occupancy      = EXCLUDED.occupancy,
                refreshed_at   = EXCLUDED.refreshed_at;
        END;
        $$;
        """
    )

    # --- property-category-month ----------------------------------------
    pcms_cols = "account_id, property_id" if with_account else "property_id"
    pcms_sel = (
        "v_account_id, property_id" if with_account else "property_id"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_property_category_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            {acct_decl}
        BEGIN
            {acct_lookup}
            DELETE FROM property_category_month_summary
            WHERE property_id = p_property_id AND month = p_month;

            INSERT INTO property_category_month_summary
                ({pcms_cols}, month, category_id, classification, amount, refreshed_at)
            SELECT {pcms_sel}, month, category_id, classification, SUM(amount), now()
            FROM v_line_item_resolved
            WHERE property_id = p_property_id AND month = p_month
            GROUP BY property_id, month, category_id, classification;
        END;
        $$;
        """
    )

    # --- unit-month ------------------------------------------------------
    # monthly_records carries account_id post-0017, so this reads it straight off mr.
    ums_cols = "account_id, unit_id" if with_account else "unit_id"
    ums_sel = "mr.account_id, mr.unit_id" if with_account else "mr.unit_id"
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_unit_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM unit_month_summary
            WHERE property_id = p_property_id AND month = p_month;

            INSERT INTO unit_month_summary
                ({ums_cols}, month, property_id, {metrics_csv}, is_vacant, refreshed_at)
            SELECT {ums_sel}, mr.month, mr.property_id, {select_metrics}, mr.is_vacant, now()
            FROM monthly_records mr
            LEFT JOIN v_monthly_pnl v
                   ON v.unit_id = mr.unit_id AND v.month = mr.month AND v.property_id = mr.property_id
            WHERE mr.property_id = p_property_id AND mr.month = p_month AND mr.unit_id IS NOT NULL;
        END;
        $$;
        """
    )

    # --- portfolio-month -------------------------------------------------
    # THE signature change: summing every property in a month is the leak this
    # migration exists to close, so the account is now a required argument.
    if with_account:
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION refresh_portfolio_month_summary(
                p_account_id UUID, p_month DATE
            ) RETURNS void
            LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO portfolio_month_summary AS s (
                    account_id, month, {metrics_csv},
                    occupied_units, total_units, occupancy, property_count, refreshed_at
                )
                SELECT
                    p_account_id, p_month,
                    {portfolio_sum},
                    COALESCE(SUM(occupied_units), 0),
                    COALESCE(SUM(total_units), 0),
                    CASE WHEN COALESCE(SUM(total_units), 0) > 0
                         THEN ROUND(SUM(occupied_units)::numeric / SUM(total_units), 5)
                         ELSE NULL END,
                    count(*),
                    now()
                FROM property_month_summary
                WHERE account_id = p_account_id AND month = p_month
                ON CONFLICT (account_id, month) DO UPDATE SET
                    {upsert_metrics},
                    occupied_units = EXCLUDED.occupied_units,
                    total_units    = EXCLUDED.total_units,
                    occupancy      = EXCLUDED.occupancy,
                    property_count = EXCLUDED.property_count,
                    refreshed_at   = EXCLUDED.refreshed_at;
            END;
            $$;
            """
        )
    else:
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION refresh_portfolio_month_summary(p_month DATE)
            RETURNS void
            LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO portfolio_month_summary AS s (
                    month, {metrics_csv},
                    occupied_units, total_units, occupancy, property_count, refreshed_at
                )
                SELECT
                    p_month,
                    {portfolio_sum},
                    COALESCE(SUM(occupied_units), 0),
                    COALESCE(SUM(total_units), 0),
                    CASE WHEN COALESCE(SUM(total_units), 0) > 0
                         THEN ROUND(SUM(occupied_units)::numeric / SUM(total_units), 5)
                         ELSE NULL END,
                    count(*),
                    now()
                FROM property_month_summary
                WHERE month = p_month
                ON CONFLICT (month) DO UPDATE SET
                    {upsert_metrics},
                    occupied_units = EXCLUDED.occupied_units,
                    total_units    = EXCLUDED.total_units,
                    occupancy      = EXCLUDED.occupancy,
                    property_count = EXCLUDED.property_count,
                    refreshed_at   = EXCLUDED.refreshed_at;
            END;
            $$;
            """
        )

    # --- wrappers --------------------------------------------------------
    # Signature unchanged, so app/summaries.py's refresh_month(property_id, month)
    # keeps working: the account is resolved from the property in here.
    portfolio_call = (
        "PERFORM refresh_portfolio_month_summary(v_account_id, p_month);"
        if with_account
        else "PERFORM refresh_portfolio_month_summary(p_month);"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_month_summaries(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            {acct_decl}
        BEGIN
            {acct_lookup}
            PERFORM refresh_property_month_summary(p_property_id, p_month);
            PERFORM refresh_property_category_month_summary(p_property_id, p_month);
            PERFORM refresh_unit_month_summary(p_property_id, p_month);
            {portfolio_call}
        END;
        $$;
        """
    )

    rebuild_portfolio_loop = (
        """
            FOR r IN SELECT DISTINCT account_id, month FROM monthly_records LOOP
                PERFORM refresh_portfolio_month_summary(r.account_id, r.month);
            END LOOP;
        """
        if with_account
        else """
            FOR r IN SELECT DISTINCT month FROM monthly_records LOOP
                PERFORM refresh_portfolio_month_summary(r.month);
            END LOOP;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION rebuild_all_summaries()
        RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE r RECORD;
        BEGIN
            TRUNCATE property_month_summary;
            TRUNCATE portfolio_month_summary;
            TRUNCATE property_category_month_summary;
            TRUNCATE unit_month_summary;
            FOR r IN SELECT DISTINCT property_id, month FROM monthly_records LOOP
                PERFORM refresh_property_month_summary(r.property_id, r.month);
                PERFORM refresh_property_category_month_summary(r.property_id, r.month);
                PERFORM refresh_unit_month_summary(r.property_id, r.month);
            END LOOP;
            {rebuild_portfolio_loop}
        END;
        $$;
        """
    )
