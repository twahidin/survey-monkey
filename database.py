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
        conn.commit()


def get_db():
    """Dependency for FastAPI - yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
