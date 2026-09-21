"""Database models for the survey chatbot application."""

import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Text, DateTime, Boolean, Integer, Float,
    ForeignKey, Enum, create_engine, Index, LargeBinary
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
    role = Column(String(20), nullable=False, server_default="admin")  # "admin" or "teacher"
    parent_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    encrypted_api_key = Column(Text, nullable=True)            # Anthropic key
    llm_provider = Column(String(20), nullable=True)           # anthropic | openrouter
    llm_model = Column(String(120), nullable=True)
    encrypted_openrouter_key = Column(Text, nullable=True)
    image_provider = Column(String(30), nullable=True)         # pollinations | openrouter | openai
    image_model = Column(String(120), nullable=True)
    image_base_url = Column(Text, nullable=True)
    encrypted_image_api_key = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    surveys = relationship("Survey", back_populates="created_by_admin")
    parent_admin = relationship("AdminUser", remote_side=[id], foreign_keys=[parent_admin_id])


class Survey(Base):
    __tablename__ = "surveys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    topic = Column(Text, nullable=False)
    system_prompt = Column(Text, nullable=False)
    facilitator_intro = Column(Text, nullable=True)  # e.g. "My name is X, and I'm working with..."
    survey_code = Column(String(50), unique=True, nullable=False, index=True)
    status = Column(Enum(SurveyStatus), default=SurveyStatus.DRAFT, nullable=False)
    max_messages = Column(Integer, default=20)  # max back-and-forth before auto-close
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime(timezone=True), nullable=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=False)
    collect_name = Column(Boolean, default=False, nullable=False, server_default="false")
    collect_email = Column(Boolean, default=False, nullable=False, server_default="false")
    collect_phone = Column(Boolean, default=False, nullable=False, server_default="false")
    survey_type = Column(String(30), nullable=True)      # "general_sensing" | "categorising" | "depth_survey"
    questions = Column(Text, nullable=True)               # what to ask participants
    instructions = Column(Text, nullable=True)            # how the bot should behave

    # --- Briefing shown before the chat (slide deck / video / document) ---
    briefing_type = Column(String(20), nullable=True)     # none | slides | video | document | link
    briefing_url = Column(Text, nullable=True)            # external URL or /api/assets/{id}
    briefing_text = Column(Text, nullable=True)           # task description shown alongside the media
    briefing_title = Column(String(255), nullable=True)

    # --- Visuals shown in the media panel while the bot asks questions ---
    image_mode = Column(String(20), nullable=True)        # none | stock | generate
    image_provider = Column(String(30), nullable=True)    # pollinations | openrouter | openai
    image_model = Column(String(120), nullable=True)
    image_base_url = Column(Text, nullable=True)          # optional OpenAI-compatible base URL
    encrypted_image_api_key = Column(Text, nullable=True)
    image_style = Column(Text, nullable=True)             # style prefix for generated images

    # --- LLM override for this survey (falls back to owner / parent / env) ---
    llm_provider = Column(String(20), nullable=True)      # anthropic | openrouter
    llm_model = Column(String(120), nullable=True)
    encrypted_llm_api_key = Column(Text, nullable=True)

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
    contact_name = Column(String(255), nullable=True)
    contact_email = Column(String(255), nullable=True)
    contact_phone = Column(String(100), nullable=True)

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


class SurveyInsight(Base):
    __tablename__ = "survey_insights"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    survey_id = Column(UUID(as_uuid=True), ForeignKey("surveys.id"), nullable=False, index=True)
    insights_json = Column(Text, nullable=False)
    generated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(64), unique=True, nullable=False, index=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=False)
    used_by_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    used_at = Column(DateTime(timezone=True), nullable=True)


class MediaAsset(Base):
    """Binary media stored in the database: generated images and uploaded briefing files."""
    __tablename__ = "media_assets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    survey_id = Column(UUID(as_uuid=True), ForeignKey("surveys.id", ondelete="CASCADE"), nullable=True, index=True)
    participant_id = Column(UUID(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=True, index=True)
    kind = Column(String(20), nullable=False)          # generated | briefing
    mime_type = Column(String(100), nullable=False)
    filename = Column(String(255), nullable=True)
    prompt = Column(Text, nullable=True)               # generation prompt, if any
    data = Column(LargeBinary, nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
