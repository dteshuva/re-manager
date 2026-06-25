"""initial schema: properties, units, categories, monthly_records, line_items,
period_status, audit_log, users + computed P&L views

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-25

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # gen_random_uuid() lives in pgcrypto on older Postgres; harmless on newer.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # --- Enums -------------------------------------------------------------
    # classification is the ONLY thing that drives the math. Adding a category never
    # touches this enum; reclassifying a category just updates categories.default_classification.
    op.execute(
        "CREATE TYPE classification AS ENUM "
        "('rent', 'operating', 'capex', 'debt_service', 'other_below_line');"
    )
    op.execute("CREATE TYPE period_status_enum AS ENUM ('draft', 'posted', 'locked');")

    # --- users -------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE users (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email           TEXT NOT NULL UNIQUE,
            hashed_password TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'member'
                            CHECK (role IN ('admin', 'member')),
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # --- properties --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE properties (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name       TEXT NOT NULL,
            type       TEXT NOT NULL CHECK (type IN ('multifamily', 'single')),
            address    TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # --- units -------------------------------------------------------------
    # units_property_id_id_key is the target of the composite FK below that keeps a
    # monthly_record's unit_id honest about its property_id.
    op.execute(
        """
        CREATE TABLE units (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            property_id UUID NOT NULL REFERENCES properties (id) ON DELETE CASCADE,
            unit_number TEXT NOT NULL,
            label       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT units_property_unit_number_key UNIQUE (property_id, unit_number),
            CONSTRAINT units_property_id_id_key UNIQUE (property_id, id)
        );
        """
    )

    # --- categories (global, shared) --------------------------------------
    op.execute(
        """
        CREATE TABLE categories (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                   TEXT NOT NULL UNIQUE,
            default_classification classification NOT NULL,
            active                 BOOLEAN NOT NULL DEFAULT TRUE,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # --- monthly_records ---------------------------------------------------
    # unit_id NULL  => property-tier record (shared capex, debt service, etc.).
    # Composite FK (property_id, unit_id) -> units(property_id, id) uses MATCH SIMPLE,
    # so it is skipped when unit_id IS NULL but enforced (unit belongs to property)
    # when set.
    op.execute(
        """
        CREATE TABLE monthly_records (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            property_id UUID NOT NULL REFERENCES properties (id) ON DELETE CASCADE,
            unit_id     UUID REFERENCES units (id) ON DELETE CASCADE,
            month       DATE NOT NULL,
            notes       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT monthly_records_month_first_check
                CHECK (date_trunc('month', month) = month),
            CONSTRAINT monthly_records_unit_in_property_fk
                FOREIGN KEY (property_id, unit_id)
                REFERENCES units (property_id, id) ON DELETE CASCADE
        );
        """
    )
    # One property-tier record per (property, month); one record per (unit, month).
    op.execute(
        """
        CREATE UNIQUE INDEX monthly_records_property_month_uidx
            ON monthly_records (property_id, month)
            WHERE unit_id IS NULL;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX monthly_records_unit_month_uidx
            ON monthly_records (property_id, unit_id, month)
            WHERE unit_id IS NOT NULL;
        """
    )

    # --- line_items --------------------------------------------------------
    # classification here is an OPTIONAL per-line override. Effective classification =
    # COALESCE(line_items.classification, categories.default_classification). The unique
    # key gives idempotent import: re-uploading a month+category overwrites in place.
    op.execute(
        """
        CREATE TABLE line_items (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            monthly_record_id UUID NOT NULL REFERENCES monthly_records (id) ON DELETE CASCADE,
            category_id       UUID NOT NULL REFERENCES categories (id),
            classification    classification,
            amount            NUMERIC(14, 2) NOT NULL DEFAULT 0,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT line_items_record_category_key UNIQUE (monthly_record_id, category_id)
        );
        """
    )

    # --- period_status -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE period_status (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            property_id UUID NOT NULL REFERENCES properties (id) ON DELETE CASCADE,
            month       DATE NOT NULL,
            status      period_status_enum NOT NULL DEFAULT 'draft',
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT period_status_property_month_key UNIQUE (property_id, month),
            CONSTRAINT period_status_month_first_check
                CHECK (date_trunc('month', month) = month)
        );
        """
    )

    # --- audit_log ---------------------------------------------------------
    op.execute(
        """
        CREATE TABLE audit_log (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    UUID REFERENCES users (id),
            action     TEXT NOT NULL,
            entity     TEXT NOT NULL,
            entity_id  TEXT,
            before     JSONB,
            after      JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # ----------------------------------------------------------------------
    #  Computed P&L views — NOI and cash flow are NEVER stored.
    # ----------------------------------------------------------------------

    # Resolve every line item to its effective classification (category default, or a
    # per-line override). This single join is why reclassifying a category recomputes
    # everything with no data migration.
    op.execute(
        """
        CREATE VIEW v_line_item_resolved AS
        SELECT
            li.id            AS line_item_id,
            li.amount        AS amount,
            COALESCE(li.classification, c.default_classification) AS classification,
            c.id             AS category_id,
            c.name           AS category_name,
            mr.property_id   AS property_id,
            mr.unit_id       AS unit_id,
            mr.month         AS month
        FROM line_items li
        JOIN categories      c  ON c.id  = li.category_id
        JOIN monthly_records mr ON mr.id = li.monthly_record_id;
        """
    )

    # Atomic P&L grain: one row per (property, unit, month). Sums are by classification,
    # so a new operating category flows into NOI and a new below-line category flows into
    # cash flow automatically. below_noi = everything that is not rent and not operating,
    # so any future classification falls below the line with zero code change.
    op.execute(
        """
        CREATE VIEW v_monthly_pnl AS
        SELECT
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
        GROUP BY property_id, unit_id, month;
        """
    )

    # Convenience portfolio rollup used by the dashboard / sample query.
    op.execute(
        """
        CREATE VIEW v_portfolio_monthly AS
        SELECT
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
        GROUP BY month
        ORDER BY month;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_portfolio_monthly;")
    op.execute("DROP VIEW IF EXISTS v_monthly_pnl;")
    op.execute("DROP VIEW IF EXISTS v_line_item_resolved;")
    op.execute("DROP TABLE IF EXISTS audit_log;")
    op.execute("DROP TABLE IF EXISTS period_status;")
    op.execute("DROP TABLE IF EXISTS line_items;")
    op.execute("DROP TABLE IF EXISTS monthly_records;")
    op.execute("DROP TABLE IF EXISTS categories;")
    op.execute("DROP TABLE IF EXISTS units;")
    op.execute("DROP TABLE IF EXISTS properties;")
    op.execute("DROP TABLE IF EXISTS users;")
    op.execute("DROP TYPE IF EXISTS period_status_enum;")
    op.execute("DROP TYPE IF EXISTS classification;")
