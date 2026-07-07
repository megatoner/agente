from sqlalchemy import Column, Integer, String, Text, DateTime, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, Session
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector
from typing import List, Optional
import json

Base = declarative_base()

EMBEDDING_DIM = 1536  # OpenAI text-embedding-3-small, ajustable


class Memory(Base):
    __tablename__ = "memories"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(255), nullable=False, index=True)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(EMBEDDING_DIM), nullable=True)
    meta_data = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def init_memory_db(engine):
    # Crear extensión vector si no existe
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(bind=engine)


def store_memory(
    db: Session,
    session_id: str,
    content: str,
    embedding: Optional[List[float]] = None,
    meta_data: Optional[dict] = None
):
    memory = Memory(
        session_id=session_id,
        content=content,
        embedding=embedding,
        meta_data=meta_data or {}
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


def search_similar(
    db: Session,
    session_id: str,
    embedding: List[float],
    limit: int = 5
) -> List[Memory]:
    # Búsqueda por similitud coseno
    results = (
        db.query(Memory)
        .filter(Memory.session_id == session_id)
        .order_by(Memory.embedding.cosine_distance(embedding))
        .limit(limit)
        .all()
    )
    return results


def get_session_memories(db: Session, session_id: str, limit: int = 50) -> List[Memory]:
    return (
        db.query(Memory)
        .filter(Memory.session_id == session_id)
        .order_by(Memory.created_at.desc())
        .limit(limit)
        .all()
    )
