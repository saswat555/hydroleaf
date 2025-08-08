# app/models.py
from datetime import datetime, timedelta, timezone
from enum import Enum as PyEnum
import uuid

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Boolean,
    ForeignKey,
    JSON,
    func,
    Index,
)
from sqlalchemy.orm import relationship, synonym
from sqlalchemy import Enum as SAEnum

from app.core.database import Base
# We intentionally import DeviceType from schemas so tests can `from app.models import DeviceType`
from app.schemas import DeviceType


# -------------------------------------------------------------------
# Helpers / Enums
# -------------------------------------------------------------------

def _uuid() -> str:
    return uuid.uuid4().hex


class TaskStatus(PyEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PaymentStatus(PyEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# -------------------------------------------------------------------
# USERS & PROFILES
# -------------------------------------------------------------------

class FarmShare(Base):
    __tablename__ = "farm_shares"

    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    farm_id = Column(String(64), ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="farm_shares")
    farm = relationship("Farm", back_populates="farm_shares")


class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    email = Column(String(128), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(50), nullable=False, default="user")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # many-to-many: farms shared *with* me
    shared_farms = relationship("Farm", secondary="farm_shares", back_populates="shared_users")
    farm_shares = relationship("FarmShare", back_populates="user", cascade="all, delete-orphan")

    # one-to-one
    profile = relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="joined",
    )

    # one-to-many
    farms = relationship("Farm", back_populates="user", cascade="all, delete-orphan", lazy="joined")
    devices = relationship("Device", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    payment_orders = relationship("PaymentOrder", back_populates="user", cascade="all, delete-orphan")
    cameras = relationship("UserCamera", back_populates="user", cascade="all, delete-orphan")


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    first_name = Column(String(50))
    last_name = Column(String(50))
    phone = Column(String(20))
    address = Column(String(256))
    city = Column(String(100))
    state = Column(String(100))
    country = Column(String(100))
    postal_code = Column(String(20))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="profile")


# -------------------------------------------------------------------
# FARMS
# -------------------------------------------------------------------

class Farm(Base):
    __tablename__ = "farms"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    owner_id = Column("user_id", String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user_id = synonym("owner_id")

    name = Column(String(128), nullable=False)
    # store as "location" in DB, but expose "address" at the ORM level for API/tests
    location = Column(String, index=True)
    address = synonym("location")
    latitude = Column(Float)
    longitude = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="farms")
    devices = relationship("Device", back_populates="farm", cascade="all, delete-orphan")

    farm_shares = relationship("FarmShare", back_populates="farm", cascade="all, delete-orphan")
    shared_users = relationship("User", secondary="farm_shares", back_populates="shared_farms")


# -------------------------------------------------------------------
# DEVICES & PROFILES
# -------------------------------------------------------------------

class Device(Base):
    __tablename__ = "devices"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    farm_id = Column(String(64), ForeignKey("farms.id", ondelete="SET NULL"), nullable=True)
    mac_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    type = Column(
        SAEnum(DeviceType, name="device_type", native_enum=False),
        nullable=False
    )
    http_endpoint = Column(String(256), nullable=False)
    location_description = Column(String(256))
    is_active = Column(Boolean, nullable=False, default=True)
    last_seen = Column(DateTime(timezone=True))
    firmware_version = Column(String(32), nullable=False, server_default="0.0.0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # JSON configs
    pump_configurations = Column(JSON)
    sensor_parameters = Column(JSON)
    valve_configurations = Column(JSON)
    switch_configurations = Column(JSON)

    user = relationship("User", back_populates="devices")
    farm = relationship("Farm", back_populates="devices")
    dosing_profiles = relationship("DosingProfile", back_populates="device", cascade="all, delete-orphan")
    sensor_readings = relationship("SensorReading", back_populates="device", cascade="all, delete-orphan")
    dosing_operations = relationship("DosingOperation", back_populates="device", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="device", cascade="all, delete-orphan")
    payment_orders = relationship("PaymentOrder", back_populates="device", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="device", cascade="all, delete-orphan")


class DosingProfile(Base):
    __tablename__ = "dosing_profiles"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    plant_name = Column(String(100), nullable=False)
    plant_type = Column(String(100), nullable=False)
    growth_stage = Column(String(50), nullable=False)
    seeding_date = Column(DateTime(timezone=True), nullable=False)
    target_ph_min = Column(Float, nullable=False)
    target_ph_max = Column(Float, nullable=False)
    target_tds_min = Column(Float, nullable=False)
    target_tds_max = Column(Float, nullable=False)
    dosing_schedule = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    device = relationship("Device", back_populates="dosing_profiles")


class DeviceCommand(Base):
    __tablename__ = "device_commands"

    id = Column(String(64), primary_key=True, default=_uuid)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    action = Column(
        SAEnum("restart", "update", name="cmd_action", native_enum=False),
        nullable=False
    )
    parameters = Column(JSON)
    issued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    dispatched = Column(Boolean, default=False)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(50), nullable=False)
    parameters = Column(JSON)

    status = Column(
        SAEnum(TaskStatus, name="task_status", native_enum=False),
        nullable=False,
        server_default=TaskStatus.PENDING.value,
        index=True,
    )
    priority = Column(Integer, nullable=False, server_default="100")
    available_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_id = Column(String(64), nullable=True, index=True)
    leased_until = Column(DateTime(timezone=True), nullable=True, index=True)
    attempts = Column(Integer, nullable=False, server_default="0")
    error_message = Column(String(255))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    result_payload = Column(JSON, nullable=True)
    device = relationship("Device", back_populates="tasks")

    __table_args__ = (
        Index("ix_tasks_device_status_avail", "device_id", "status", "available_at"),
        Index("ix_tasks_device_lease", "device_id", "lease_id"),
    )


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    reading_type = Column(String(50), nullable=False)
    value = Column(Float, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    location = Column(String(100))

    device = relationship("Device", back_populates="sensor_readings")


class DosingOperation(Base):
    __tablename__ = "dosing_operations"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    operation_id = Column(String(100), unique=True, nullable=False)
    actions = Column(JSON, nullable=False)
    status = Column(String(50), nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    device = relationship("Device", back_populates="dosing_operations")


# -------------------------------------------------------------------
# PLANTS & ANALYSIS
# -------------------------------------------------------------------

class Plant(Base):
    __tablename__ = "plants"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    farm_id = Column(String(64), ForeignKey("farms.id", ondelete="CASCADE"), nullable=True)

    name = Column(String(100), nullable=False)
    type = Column(String(100), nullable=False)
    growth_stage = Column(String(50), nullable=False)
    seeding_date = Column(DateTime(timezone=True), nullable=False)
    region = Column(String(100), nullable=False)

    # persist as "location"; expose "location_description" at ORM level
    location = Column(String(100), nullable=False)
    location_description = synonym("location")

    target_ph_min = Column(Float)
    target_ph_max = Column(Float)
    target_tds_min = Column(Float)
    target_tds_max = Column(Float)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SupplyChainAnalysis(Base):
    __tablename__ = "supply_chain_analysis"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    origin = Column(String(100), nullable=False)
    destination = Column(String(100), nullable=False)
    produce_type = Column(String(50), nullable=False)
    weight_kg = Column(Float, nullable=False)
    transport_mode = Column(String(50), server_default="railway", nullable=False)
    distance_km = Column(Float, nullable=False)
    cost_per_kg = Column(Float, nullable=False)
    total_cost = Column(Float, nullable=False)
    estimated_time_hours = Column(Float, nullable=False)
    market_price_per_kg = Column(Float, nullable=False)
    net_profit_per_kg = Column(Float, nullable=False)
    final_recommendation = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    conversation_logs = relationship("ConversationLog", back_populates="analysis", cascade="all, delete-orphan")


class ConversationLog(Base):
    __tablename__ = "conversation_logs"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    analysis_id = Column(String(64), ForeignKey("supply_chain_analysis.id", ondelete="SET NULL"))
    conversation = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    analysis = relationship("SupplyChainAnalysis", back_populates="conversation_logs")


# -------------------------------------------------------------------
# SUBSCRIPTIONS & BILLING
# -------------------------------------------------------------------

class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    name = Column(String(128), nullable=False)
    device_types = Column(JSON, nullable=False)  # e.g. ["dosing_unit"]
    # NEW: enforce per-plan device limit (used heavily in tests)
    device_limit = Column(Integer, nullable=False, default=1)
    duration_days = Column(Integer, nullable=False)  # e.g. 30
    price = Column(Integer, nullable=False)
    created_by = Column(String(64), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    activation_keys = relationship("ActivationKey", back_populates="plan", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="plan", cascade="all, delete-orphan")
    payment_orders = relationship("PaymentOrder", back_populates="plan", cascade="all, delete-orphan")


class ActivationKey(Base):
    __tablename__ = "activation_keys"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    key = Column(String(64), unique=True, nullable=False, index=True)
    device_type = Column(
        SAEnum(DeviceType, name="activation_device_type", native_enum=False),
        nullable=False
    )
    plan_id = Column(String(64), ForeignKey("subscription_plans.id", ondelete="CASCADE"), nullable=False)
    created_by = Column(String(64), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    redeemed = Column(Boolean, default=False, nullable=False)
    redeemed_at = Column(DateTime(timezone=True))
    redeemed_device_id = Column(String(64), ForeignKey("devices.id", ondelete="SET NULL"))
    redeemed_user_id = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"))
    allowed_device_id = Column(String(64), ForeignKey("devices.id", ondelete="SET NULL"))

    plan = relationship("SubscriptionPlan", back_populates="activation_keys")
    creator = relationship("Admin", foreign_keys=[created_by])
    redeemed_device = relationship("Device", foreign_keys=[redeemed_device_id])
    redeemed_user = relationship("User", foreign_keys=[redeemed_user_id])
    allowed_device = relationship("Device", foreign_keys=[allowed_device_id], backref="allowed_activation_keys")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    plan_id = Column(String(64), ForeignKey("subscription_plans.id", ondelete="SET NULL"), nullable=True)
    start_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    # NEW: current effective device_limit for this subscription (can be extended)
    device_limit = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="subscriptions")
    device = relationship("Device", back_populates="subscriptions")
    plan = relationship("SubscriptionPlan", back_populates="subscriptions")


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    plan_id = Column(String(64), ForeignKey("subscription_plans.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Integer, nullable=False)
    status = Column(
        SAEnum(PaymentStatus, name="payment_status", native_enum=False),
        default=PaymentStatus.PENDING,
        nullable=False
    )
    upi_transaction_id = Column(String(64))
    screenshot_path = Column(String(256))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="payment_orders")
    device = relationship("Device", back_populates="payment_orders")
    plan = relationship("SubscriptionPlan", back_populates="payment_orders")


# -------------------------------------------------------------------
# CAMERAS & DETECTIONS
# -------------------------------------------------------------------

class Camera(Base):
    __tablename__ = "cameras"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    is_online = Column(Boolean, default=False, nullable=False)
    last_seen = Column(DateTime(timezone=True))
    frames_received = Column(Integer, default=0, nullable=False)
    clips_count = Column(Integer, default=0, nullable=False)
    last_clip_time = Column(DateTime(timezone=True))
    storage_used = Column(Float, default=0.0, nullable=False)  # MB
    settings = Column(JSON)
    hls_path = Column(String(256))
    user_cameras = relationship("UserCamera", back_populates="camera", cascade="all, delete-orphan")
    detection_records = relationship("DetectionRecord", back_populates="camera", cascade="all, delete-orphan")


class UserCamera(Base):
    __tablename__ = "user_cameras"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    camera_id = Column(String(64), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    nickname = Column(String(120))

    user = relationship("User", back_populates="cameras")
    camera = relationship("Camera", back_populates="user_cameras")


class DetectionRecord(Base):
    __tablename__ = "detection_records"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    camera_id = Column(String(64), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    object_name = Column(String(100), nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    camera = relationship("Camera", back_populates="detection_records")


class CloudKey(Base):
    __tablename__ = "cloud_keys"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    key = Column(String(64), unique=True, nullable=False, index=True)
    created_by = Column(String(64), ForeignKey("admins.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    creator = relationship("Admin", back_populates="cloud_keys")
    usages = relationship("CloudKeyUsage", back_populates="cloud_key", cascade="all, delete-orphan", lazy="selectin")


class CameraToken(Base):
    __tablename__ = "camera_tokens"

    camera_id = Column(String(64), primary_key=True)
    token = Column(String(64), nullable=False)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())


class Admin(Base):
    __tablename__ = "admins"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    email = Column(String(128), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(50), nullable=False, default="superadmin")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cloud_keys = relationship("CloudKey", back_populates="creator", cascade="all, delete-orphan", lazy="selectin")

    @property
    def profile(self):
        # Satisfy places that might access Admin.profile like User.profile
        return None


class ValveState(Base):
    __tablename__ = "valve_states"

    device_id = Column(String(64), primary_key=True, index=True)
    states = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SwitchState(Base):
    __tablename__ = "switch_states"

    device_id = Column(String(64), primary_key=True, index=True)
    states = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CloudKeyUsage(Base):
    __tablename__ = "cloud_key_usages"

    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    cloud_key_id = Column(String(64), ForeignKey("cloud_keys.id", ondelete="CASCADE"), nullable=False)
    resource_id = Column(String(64), nullable=False)  # device_id or camera_id
    used_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    cloud_key = relationship("CloudKey", back_populates="usages")


# -------------------------------------------------------------------
# DEVICE TOKENS (generic)
# -------------------------------------------------------------------

class DeviceToken(Base):
    __tablename__ = "device_tokens"

    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    device_type = Column(
        SAEnum(DeviceType, name="token_device_type", native_enum=False),
        nullable=False
    )
    issued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc) + timedelta(days=30),
    )

    device = relationship("Device", lazy="joined")
