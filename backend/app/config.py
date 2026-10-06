from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://postgres@localhost:5432/re_manager"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 43200  # 30 days

    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "admin12345"

    frontend_origin: str = "http://localhost:5173"

    # PDF statement extraction (POST /import/statement/extract). Two free backends:
    #   * heuristic (pdfplumber) — always available, zero setup, zero cost.
    #   * a LOCAL LLM via Ollama — used only when reachable, for messy/free-text layouts.
    # Both run entirely on-machine; there is no paid, per-use API involved.
    # Set statement_use_ollama=false to force the heuristic even if Ollama is running.
    statement_use_ollama: bool = True
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    # Hard cap so an accidental 500-page upload can't wedge the parser.
    statement_max_pages: int = 20
    # Max PDFs accepted in a single batch upload.
    statement_max_files: int = 20

    # Attention-feed thresholds. The PERCENTAGE is the primary, size-independent trigger;
    # the absolute-$ floor is an optional materiality gate (0 = pure percentage). These are
    # only fallbacks — the live values come from the per-account attention_settings row.
    attention_noi_drop_min_abs: float = 0.0
    attention_noi_drop_min_pct: float = 15.0
    attention_expense_spike_min_abs: float = 0.0
    attention_expense_spike_min_pct: float = 100.0
    attention_unit_noi_drop_min_abs: float = 0.0
    attention_unit_noi_drop_min_pct: float = 15.0
    attention_unit_expense_spike_min_abs: float = 0.0
    attention_unit_expense_spike_min_pct: float = 100.0
    # Vacancy: flag when a property's occupancy falls by >= this many percentage points
    # (size-independent — one unit is 2.5% of a 40-unit property but 0.1% of a 1,000-unit one).
    attention_vacancy_min_occupancy_drop_pct: float = 2.0
    # Absolute vacancy: flag any property whose vacancy rate (100 - occupancy%) is >= this,
    # regardless of month-over-month change — a persistently very-empty property is a standing
    # problem even in a month where it didn't get worse.
    attention_vacancy_high_absolute_pct: float = 20.0
    # Arrears (migration 0025): flag a tenancy whose CUMULATIVE balance is at least this many
    # pounds/dollars, OR at least this many months of its own rent. OR, not AND, and
    # deliberately unlike every threshold above it: the other detectors are CHANGE detectors
    # that need a materiality gate in both dimensions to stay quiet, whereas either of these
    # conditions alone is already a tenancy worth chasing — £1,200 owed matters whatever the
    # rent, and a tenant a full month down matters even on a £450 room. Global, not
    # per-account (no attention_settings column): same treatment as the reconcile ratio below,
    # and promotable to per-account later without touching the detector.
    attention_arrears_min_balance: float = 500.0
    attention_arrears_min_months: float = 1.0
    # How many individual debtors the feed names before collapsing the tail. Arrears is a
    # STANDING balance, so unlike the change detectors it qualifies every month it goes unpaid
    # and a portfolio with a long tail of small debts would otherwise fill all 50 feed slots
    # with arrears and bury the NOI drops and vacancies entirely. The worst N are named; the
    # remainder become one row per property ("N units in arrears, £X total"), which is also
    # the more actionable shape for a tail of small balances.
    attention_arrears_max_items: int = 10
    # Root-cause linking: merge a property-month's NOI drop INTO its expense spike when the
    # prior-month operating-expense increase accounts for >= this share of the NOI decline
    # (magnitude reconciliation — near-equality is the evidence they're the same event).
    attention_noi_expense_reconcile_ratio: float = 0.8


@lru_cache
def get_settings() -> Settings:
    return Settings()
