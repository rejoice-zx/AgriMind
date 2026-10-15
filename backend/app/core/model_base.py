"""Model metadata without loading deployment settings or opening a database."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
