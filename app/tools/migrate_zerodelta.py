"""Additive migration 001: python -m app.tools.migrate_zerodelta (LOCAL DATABASE_URL only)."""

from sqlalchemy import create_engine
from app.core.trading_config import TradingSettings
from app.persistence.models import ZeroDeltaSummaryBatch


def main():
    engine = create_engine(TradingSettings().database_url)
    try:
        with engine.begin() as connection:
            ZeroDeltaSummaryBatch.__table__.create(connection, checkfirst=True)
        print("Migration 001: local zerodelta_summary_batches table ready")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
