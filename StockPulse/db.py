import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# PostgreSQL is the default runtime database; override with STOCKPULSE_DATABASE_URL when needed.
DATABASE_URL = os.getenv("STOCKPULSE_DATABASE_URL", "postgresql+psycopg://stockpulse:stockpulse@localhost:5432/stockpulse")

engine_kwargs = {"future": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(bind=engine)
