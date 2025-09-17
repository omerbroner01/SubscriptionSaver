# db_patches.py
from sqlalchemy import text

def run_db_patches(db):
    """
    Idempotent DB patches that can run safely on each deploy.
    """
    with db.engine.begin() as conn:
        # Add created_at if it's missing (Postgres safe)
        conn.execute(
            text('ALTER TABLE "user" '
                 'ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()')
        )
        # Optional: make sure email is indexed/unique (no-op if it already exists)
        conn.execute(
            text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_email ON "user"(email)')
        )
