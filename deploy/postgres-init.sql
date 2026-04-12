-- Postgres init script run once on first container boot.
--
-- Installs the extensions the schema migrations expect to already
-- exist (alembic migrations CREATE EXTENSION ... IF NOT EXISTS, but
-- having them here means a fresh DB is ready before alembic runs).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
