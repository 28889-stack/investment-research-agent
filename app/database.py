from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_db_engine(database_url: str) -> Engine:
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}

    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_database(engine: Engine) -> None:
    from app import models  # noqa: F401

    if engine.dialect.name != "sqlite":
        Base.metadata.create_all(engine)
        return

    # The Web and Worker may start together. BEGIN IMMEDIATE serializes schema
    # inspection and DDL; every process re-checks columns after obtaining the lock.
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            Base.metadata.create_all(connection)
            inspector = inspect(connection)
            existing = {
                column["name"]
                for column in inspector.get_columns("research_runs")
            }
            additions = {
                "workflow_name": "VARCHAR(80)",
                "workflow_version": "VARCHAR(32)",
                "checkpoint_thread_id": "VARCHAR(64)",
                "current_node": "VARCHAR(100)",
                "runtime_mode": "VARCHAR(16)",
                "resolved_symbol": "VARCHAR(64)",
                "security_name": "VARCHAR(100)",
                "data_version": "VARCHAR(160)",
            }
            for column_name, column_type in additions.items():
                if column_name not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE research_runs ADD COLUMN {column_name} {column_type}"
                        )
                    )
            tables = set(inspect(connection).get_table_names())
            if "agent_executions" in tables:
                agent_columns = {
                    column["name"]
                    for column in inspect(connection).get_columns("agent_executions")
                }
                agent_additions = {
                    "input_tokens": "INTEGER",
                    "output_tokens": "INTEGER",
                    "total_tokens": "INTEGER",
                    "estimated_cost": "FLOAT",
                    "cost_currency": "VARCHAR(12)",
                }
                for column_name, column_type in agent_additions.items():
                    if column_name not in agent_columns:
                        connection.execute(
                            text(
                                f"ALTER TABLE agent_executions ADD COLUMN {column_name} {column_type}"
                            )
                        )
            if "run_events" in tables:
                event_columns = {
                    column["name"]
                    for column in inspect(connection).get_columns("run_events")
                }
                if "event_key" not in event_columns:
                    connection.execute(
                        text("ALTER TABLE run_events ADD COLUMN event_key VARCHAR(240)")
                    )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ux_run_events_event_key "
                        "ON run_events (event_key)"
                    )
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def session_dependency(
    session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
