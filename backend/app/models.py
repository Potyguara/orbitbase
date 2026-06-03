from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    jobs: Mapped[list["VRSJob"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    usage_records: Mapped[list["UsageRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    vrs_processing_profiles: Mapped[list["VrsProcessingProfile"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    advanced_credentials: Mapped[Optional["UserAdvancedCredentials"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )


class UserAdvancedCredentials(Base):
    __tablename__ = "user_advanced_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_advanced_credentials_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )

    earthdata_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    earthdata_password: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="advanced_credentials")


class VRSJob(Base):
    __tablename__ = "vrs_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)

    output_rinex_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    output_marker_name: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # legado / compatibilidade
    rover_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    upload_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detected_observation_file: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    uploaded_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    observation_file_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )

    approx_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    approx_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    requested_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    export_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    rinex_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    preprocess_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    preprocess_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    marker_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rinex_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    approx_position_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_epoch_text: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_epoch_text: Mapped[str | None] = mapped_column(String(50), nullable=True)

    processing_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    processing_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    solution_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processing_mode: Mapped[str | None] = mapped_column(String(50), nullable=True)

    target_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_height: Mapped[float | None] = mapped_column(Float, nullable=True)

    session_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    occupation_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # legado / compatibilidade com processamento anterior
    base_1_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_1_detected_observation_file: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    base_1_uploaded_file_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    base_1_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_1_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    base_2_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_2_detected_observation_file: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    base_2_uploaded_file_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    base_2_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_2_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    base_3_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_3_detected_observation_file: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    base_3_uploaded_file_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    base_3_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_3_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    reference_base_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    network_validation_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    network_validation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # novo fluxo: seleção de estações no mapa
    selected_station_codes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_station_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    # perfil VRS efetivamente usado no job
    vrs_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vrs_profile_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    vrs_profile_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    vrs_profile_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="jobs")


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )

    ref_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    included_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_exports: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_exports: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped["User"] = relationship(back_populates="usage_records")


class VrsProcessingProfile(Base):
    __tablename__ = "vrs_processing_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # system_default | system_suggested | user_custom
    profile_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="user_custom", index=True
    )

    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    config_json: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    user: Mapped[Optional["User"]] = relationship(
        back_populates="vrs_processing_profiles"
    )


class GnssStation(Base):
    __tablename__ = "gnss_stations"
    __table_args__ = (
        UniqueConstraint("station_code", name="uq_gnss_station_station_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    station_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    station_name: Mapped[str] = mapped_column(String(120), nullable=False)

    city: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)

    sat_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    international_code: Mapped[str | None] = mapped_column(
        String(30), nullable=True, index=True
    )

    latitude: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    ellipsoidal_height: Mapped[float | None] = mapped_column(Float, nullable=True)

    x: Mapped[float | None] = mapped_column(Float, nullable=True)
    y: Mapped[float | None] = mapped_column(Float, nullable=True)
    z: Mapped[float | None] = mapped_column(Float, nullable=True)

    utm_n: Mapped[float | None] = mapped_column(Float, nullable=True)
    utm_e: Mapped[float | None] = mapped_column(Float, nullable=True)
    mc: Mapped[int | None] = mapped_column(Integer, nullable=True)

    receiver_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    receiver_serial: Mapped[str | None] = mapped_column(String(80), nullable=True)
    receiver_firmware: Mapped[str | None] = mapped_column(String(80), nullable=True)

    antenna_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    antenna_serial: Mapped[str | None] = mapped_column(String(80), nullable=True)
    antenna_height: Mapped[float | None] = mapped_column(Float, nullable=True)

    info_additional: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    is_in_warning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_operational: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    supports_rinex_15s: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    supports_rinex_1s: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    supports_rinex_2: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    supports_rinex_3: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    last_firmware_update: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_station_update: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    report_reference_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    report_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloads_page_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(String(30), nullable=False, default="IBGE_RBMC")
    sync_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="synced"
    )
    sync_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
