"""Database models for the survey chatbot application."""

import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Text, DateTime, Boolean, Integer, Float,
    ForeignKey, Enum, create_engine, Index
)
try:
    from sqlalchemy.orm import declarative_base
except ImportError:
    from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.dialects.postgresql import UUID

Base = declarative_base()


class SurveyStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"


class ParticipantStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    surveys = relationship("Survey", back_populates="created_by_admin")


class Survey(Base):
    __tablename__ = "surveys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    topic = Column(Text, nullable=False)
    system_prompt = Column(Text, nullable=False)
    survey_code = Column(String(50), unique=True, nullable=False, index=True)
    status = Column(Enum(SurveyStatus), default=SurveyStatus.DRAFT, nullable=False)
    max_messages = Column(Integer, default=20)  # max back-and-forth before auto-close
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime(timezone=True), nullable=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=False)

    created_by_admin = relationship("AdminUser", back_populates="surveys")
    participants = relationship("Participant", back_populates="survey", cascade="all, delete-orphan")

    @property
    def active_participants_count(self):
        return sum(1 for p in self.participants if p.status == ParticipantStatus.ACTIVE)

    @property
    def completed_participants_count(self):
        return sum(1 for p in self.participants if p.status == ParticipantStatus.COMPLETED)

    @property
    def total_participants_count(self):
        return len(self.participants)


class Participant(Base):
    __tablename__ = "participants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    survey_id = Column(UUID(as_uuid=True), ForeignKey("surveys.id"), nullable=False)
    session_token = Column(String(100), unique=True, nullable=False, index=True)
    status = Column(Enum(ParticipantStatus), default=ParticipantStatus.ACTIVE, nullable=False)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)

    survey = relationship("Survey", back_populates="participants")
    messages = relationship("ChatMessage", back_populates="participant", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_participants_survey_status", "survey_id", "status"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id = Column(UUID(as_uuid=True), ForeignKey("participants.id"), nullable=False)
    role = Column(String(20), nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    participant = relationship("Participant", back_populates="messages")

    __table_args__ = (
        Index("ix_chat_messages_participant_created", "participant_id", "created_at"),
    )


# --- Analysis chat (admin chatbot for insights) ---

class AnalysisMessage(Base):
    __tablename__ = "analysis_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    survey_id = Column(UUID(as_uuid=True), ForeignKey("surveys.id"), nullable=False)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
