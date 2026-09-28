from alembic import context
from app.core.config import settings
from app.db.session import Base, make_engine
import app.models  # noqa: F401


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name.startswith("knowledge_fts"):
        return False
    return True


if context.is_offline_mode():
    context.configure(url=settings().database_url, target_metadata=Base.metadata, literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()
else:
    with make_engine(settings().database_url).connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata,
                  render_as_batch=connection.dialect.name == "sqlite", include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()
