from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.core.config import settings


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def make_engine(url):
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_config(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
    return engine


engine = make_engine(settings().database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction():
    with SessionLocal() as db:
        with db.begin():
            yield db


def get_db():
    with SessionLocal() as db:
        yield db
