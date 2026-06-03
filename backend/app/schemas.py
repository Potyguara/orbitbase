from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class MessageResponse(BaseModel):
    message: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    email: EmailStr
    password: str = Field(min_length=6, max_length=100)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserRead(BaseModel):
    id: int
    name: str
    email: EmailStr
    is_active: bool
    plan: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VRSJobCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)

    output_rinex_name: str | None = Field(default=None, max_length=120)
    output_marker_name: str | None = Field(default=None, max_length=40)

    approx_lat: float | None = None
    approx_lon: float | None = None

    requested_minutes: int = Field(default=0, ge=0, le=1440)
    notes: str | None = None

    target_lat: float | None = None
    target_lon: float | None = None
    target_height: float | None = None

    session_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    occupation_minutes: int | None = Field(default=None, ge=1, le=1440)

    selected_station_codes: list[str] | None = None


class VRSJobUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)

    output_rinex_name: str | None = Field(default=None, max_length=120)
    output_marker_name: str | None = Field(default=None, max_length=40)

    approx_lat: float | None = None
    approx_lon: float | None = None

    requested_minutes: int | None = Field(default=None, ge=0, le=1440)
    notes: str | None = None

    target_lat: float | None = None
    target_lon: float | None = None
    target_height: float | None = None

    session_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    occupation_minutes: int | None = Field(default=None, ge=1, le=1440)

    status: Literal["draft", "queued", "running", "completed", "failed"] | None = None

    selected_station_codes: list[str] | None = None


class VRSJobRead(BaseModel):
    id: int
    name: str

    output_rinex_name: str | None = None
    output_marker_name: str | None = None

    approx_lat: float | None = None
    approx_lon: float | None = None

    requested_minutes: int
    generated_minutes: int
    export_count: int

    status: str
    rinex_path: str | None = None
    notes: str | None = None

    created_at: datetime
    updated_at: datetime

    preprocess_status: str | None = None
    preprocess_summary: str | None = None

    observation_file_path: str | None = None
    marker_name: str | None = None
    rinex_version: str | None = None
    approx_position_text: str | None = None
    interval_seconds: float | None = None
    first_epoch_text: str | None = None
    last_epoch_text: str | None = None

    processing_status: str | None = None
    processing_summary: str | None = None
    solution_path: str | None = None

    target_lat: float | None = None
    target_lon: float | None = None
    target_height: float | None = None

    session_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    occupation_minutes: int | None = None

    selected_station_codes: list[str] | None = None
    selected_station_count: int | None = None

    network_validation_status: str | None = None
    network_validation_summary: str | None = None

    status_label: str | None = None
    network_status_label: str | None = None
    processing_status_label: str | None = None
    progress_percent: int | None = None
    progress_label: str | None = None
    next_step_hint: str | None = None

    can_validate_network: bool | None = None
    can_process: bool | None = None
    can_export: bool | None = None

    processing_mode: str | None = None

    base_1_lat: float | None = None
    base_1_lon: float | None = None
    base_2_lat: float | None = None
    base_2_lon: float | None = None
    base_3_lat: float | None = None
    base_3_lon: float | None = None

    reference_base_name: str | None = None

    vrs_profile_name: str | None = None
    vrs_profile_type: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UsageRead(BaseModel):
    ref_month: str
    included_minutes: int
    used_minutes: int
    included_exports: int
    used_exports: int

    model_config = ConfigDict(from_attributes=True)


class ProcessResponse(BaseModel):
    job_id: int
    processing_status: str
    processing_summary: str
    solution_path: str | None = None


class NetworkValidationResponse(BaseModel):
    job_id: int
    network_validation_status: str
    network_validation_summary: str


# =========================================================
# GNSS STATIONS
# =========================================================


class GnssStationRead(BaseModel):
    id: int
    station_code: str
    station_name: str
    city: str | None = None
    state: str | None = None

    sat_code: str | None = None
    international_code: str | None = None

    latitude: float | None = None
    longitude: float | None = None
    ellipsoidal_height: float | None = None

    x: float | None = None
    y: float | None = None
    z: float | None = None

    utm_n: float | None = None
    utm_e: float | None = None
    mc: int | None = None

    receiver_model: str | None = None
    receiver_serial: str | None = None
    receiver_firmware: str | None = None

    antenna_model: str | None = None
    antenna_serial: str | None = None
    antenna_height: float | None = None

    is_active: bool
    is_in_warning: bool
    is_operational: bool

    supports_rinex_1s: bool
    supports_rinex_15s: bool
    supports_rinex_2: bool
    supports_rinex_3: bool

    report_url: str | None = None
    downloads_page_url: str | None = None

    source: str | None = None
    sync_status: str | None = None
    sync_message: str | None = None
    last_synced_at: datetime | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class RbmcSyncErrorItem(BaseModel):
    report_url: str
    error: str


class RbmcSyncResponse(BaseModel):
    downloads_url: str
    reports_found: int
    inserted: int
    updated: int
    failed: int
    errors: list[RbmcSyncErrorItem] = []


# =========================================================
# VRS PROCESSING PROFILES
# =========================================================


class VrsPppTrialSchema(BaseModel):
    strategy: str
    min_consecutive_epochs: int = Field(ge=1)
    min_satellites_per_epoch: int = Field(ge=1)
    min_priority_obs_per_satellite_avg: float = Field(ge=0)
    max_gap_factor: float = Field(ge=0)


class VrsProfileConfigSchema(BaseModel):
    min_bases_per_observation: int = Field(default=2, ge=1, le=10)
    min_satellites_per_epoch: int = Field(default=5, ge=1, le=99)
    max_satellites_per_epoch: int = Field(default=10, ge=1, le=99)
    min_satellite_presence_ratio: float = Field(default=0.45, ge=0.0, le=1.0)
    min_satellite_longest_arc: int = Field(default=80, ge=1)
    local_continuity_window: int = Field(default=2, ge=0)
    min_valid_in_local_window: int = Field(default=4, ge=1)

    prefer_nearest_base: bool = True
    strong_nearest_base_bias: bool = True
    require_dual_frequency: bool = True
    preserve_full_user_window: bool = True
    generate_ppp_aux_file: bool = True

    priority_obs: list[str] = Field(
        default_factory=lambda: [
            "P1",
            "C1",
            "L1",
            "P2",
            "C2",
            "L2",
            "C5",
            "S1",
            "S2",
            "S5",
        ]
    )

    ppp_trials: list[VrsPppTrialSchema] = Field(
        default_factory=lambda: [
            VrsPppTrialSchema(
                strategy="strict",
                min_consecutive_epochs=120,
                min_satellites_per_epoch=8,
                min_priority_obs_per_satellite_avg=4.0,
                max_gap_factor=2.0,
            ),
            VrsPppTrialSchema(
                strategy="balanced",
                min_consecutive_epochs=60,
                min_satellites_per_epoch=6,
                min_priority_obs_per_satellite_avg=3.0,
                max_gap_factor=2.5,
            ),
            VrsPppTrialSchema(
                strategy="short_rover",
                min_consecutive_epochs=20,
                min_satellites_per_epoch=4,
                min_priority_obs_per_satellite_avg=2.0,
                max_gap_factor=3.0,
            ),
        ]
    )


class VrsProfileCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=255)
    config: VrsProfileConfigSchema | dict[str, Any]
    activate: bool = False


class VrsProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=255)
    config: VrsProfileConfigSchema | dict[str, Any] | None = None


class VrsProfileRead(BaseModel):
    id: int
    user_id: int | None = None

    name: str
    description: str | None = None
    config: dict[str, Any]

    is_system: bool
    is_active: bool

    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UploadResponse(BaseModel):
    filename: str
    stored_path: str
    size_bytes: int


class ExtractedFileResponse(BaseModel):
    original_filename: str
    extracted_files: list[str]
    detected_observation_file: str | None = None
    detected_observation_path: str | None = None


class PreprocessResponse(BaseModel):
    job_id: int
    preprocess_status: str
    preprocess_summary: str
    observation_file_path: str | None = None
    marker_name: str | None = None
    rinex_version: str | None = None
    approx_position_text: str | None = None
    interval_seconds: float | None = None
    first_epoch_text: str | None = None
    last_epoch_text: str | None = None


class ProcessResponse(BaseModel):
    job_id: int
    processing_status: str
    processing_summary: str
    solution_path: str | None = None


class NetworkValidationResponse(BaseModel):
    job_id: int
    network_validation_status: str
    network_validation_summary: str


class UserAdvancedCredentialsUpsert(BaseModel):
    earthdata_username: str = Field(min_length=1, max_length=255)
    earthdata_password: str = Field(min_length=1, max_length=1000)


class UserAdvancedCredentialsRead(BaseModel):
    earthdata_configured: bool
    earthdata_username_masked: str | None = None
    updated_at: datetime | None = None


class UserAdvancedCredentialsDeleteResponse(BaseModel):
    ok: bool
    message: str
