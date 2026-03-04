"""Database connection and session management."""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/survey_db"
)

# Railway uses DATABASE_URL with postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
# Fix typo: Ppostgresql (e.g. from variable reference) -> postgresql
if DATABASE_URL.startswith("Ppostgresql://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("Ppostgresql://"):]

# Pool sized for many concurrent survey participants (each streaming chat holds a connection)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=25, max_overflow=75)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create all tables and run any one-off migrations."""
    Base.metadata.create_all(bind=engine)
    # Add facilitator_intro if missing (existing DBs)
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS facilitator_intro TEXT"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS survey_insights ("
            "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
            "survey_id UUID NOT NULL REFERENCES surveys(id), "
            "insights_json TEXT NOT NULL, "
            "generated_at TIMESTAMPTZ DEFAULT now())"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_survey_insights_survey_id ON survey_insights(survey_id)"
        ))
        # Contact collection columns
        for col in ["collect_name", "collect_email", "collect_phone"]:
            conn.execute(text(
                f"ALTER TABLE surveys ADD COLUMN IF NOT EXISTS {col} BOOLEAN NOT NULL DEFAULT false"
            ))
        # Survey wizard columns
        for col in ["survey_type VARCHAR(30)", "questions TEXT", "instructions TEXT"]:
            conn.execute(text(f"ALTER TABLE surveys ADD COLUMN IF NOT EXISTS {col}"))
        # Participant contact columns
        for col, coltype in [("contact_name", "VARCHAR(255)"), ("contact_email", "VARCHAR(255)"), ("contact_phone", "VARCHAR(100)")]:
            conn.execute(text(
                f"ALTER TABLE participants ADD COLUMN IF NOT EXISTS {col} {coltype}"
            ))
        # Multi-tenant columns
        conn.execute(text(
            "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'admin'"
        ))
        conn.execute(text(
            "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS parent_admin_id UUID REFERENCES admin_users(id)"
        ))
        conn.execute(text(
            "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS encrypted_api_key TEXT"
        ))
        # Invite codes table
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS invite_codes ("
            "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
            "code VARCHAR(64) UNIQUE NOT NULL, "
            "admin_id UUID NOT NULL REFERENCES admin_users(id), "
            "used_by_id UUID REFERENCES admin_users(id), "
            "created_at TIMESTAMPTZ DEFAULT now(), "
            "used_at TIMESTAMPTZ)"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_invite_codes_code ON invite_codes(code)"))
        conn.commit()


def get_db():
    """Dependency for FastAPI - yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
