"""Portable SQLAlchemy types used by the claim substrate."""

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")
