"""SQLAlchemy engine and session configuration."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import PROJECT_ROOT, get_settings


class Base(DeclarativeBase):
    """Declarative base shared by database models."""


def _build_engine(database_url: str) -> Engine:
    """Build a SQLAlchemy engine with SQLite and Windows-safe paths."""
    connect_args: dict[str, object] = {}
    engine_options: dict[str, object] = {"pool_pre_ping": True}
    if database_url.startswith(("postgres://", "postgresql://")):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1) if database_url.startswith("postgres://") else database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql+"):
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        parts = urlsplit(database_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if parts.hostname not in {"localhost", "127.0.0.1"}:
            query.setdefault("sslmode", "require")
        database_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        engine_options.update(pool_size=5, max_overflow=5, pool_timeout=30, pool_recycle=1800)
        connect_args.update(connect_timeout=10, prepare_threshold=None)
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool
        elif database_url.startswith("sqlite:///./"):
            relative_path = database_url.removeprefix("sqlite:///./")
            db_path = PROJECT_ROOT / relative_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite:///{db_path.as_posix()}"
    db_engine = create_engine(database_url, connect_args=connect_args, **engine_options)
    if database_url.startswith("sqlite"):
        @event.listens_for(db_engine, "connect")
        def _enable_foreign_keys(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return db_engine


engine = _build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a database session and always close it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def transaction(session: Session) -> Iterator[Session]:
    """Context manager for an atomic unit of work with rollback on failure."""
    try:
        if session.in_transaction():
            # Reads trigger SQLAlchemy autobegin; wrap such read-then-write units
            # in a savepoint, then commit the enclosing session transaction.
            with session.begin_nested():
                yield session
            session.commit()
        else:
            with session.begin():
                yield session
    except Exception:
        session.rollback()
        raise


def database_health() -> bool:
    """Check database connectivity without creating or mutating schema."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
