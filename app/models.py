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
    text
)
from sqlalchemy.orm import relationship
from sqlalchemy import Enum as Enum 
from app.core.database import Base
from app.schemas import DeviceType


# -------------------------------------------------------------------
# USERS & PROFILES
# -------------------------------------------------------------------

def _uuid() -> str:
    return uuid.uuid4().hex 

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(128), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role            = Column(String(50), nullable=False, default="user")
    created_at      = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at      = Column(
                         DateTime(timezone=True),
                         server_default=func.now(),
                         onupdate=func.now(),
                         nullable=False,
                     )

    # one‐to‐one
    profile      = relationship(
                       "UserProfile",
                       back_populates="user",
                       uselist=False,
                       cascade="all, delete-orphan",
                       lazy="joined",
                   )
    # one‐to‐many
    farms        = relationship(
                       "Farm",
                       back_populates="user",
                       cascade="all, delete-orphan",
                       lazy="joined",
                   )
    devices      = relationship("Device", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    payment_orders = relationship("PaymentOrder", back_populates="user", cascade="all, delete-orphan")
    cameras      = relationship("UserCamera", back_populates="user", cascade="all, delete-orphan")

class UserProfile(Base):
    __tablename__ = "user_profiles"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    first_name  = Column(String(50))
    last_name   = Column(String(50))
    phone       = Column(String(20))
    address     = Column(String(256))
    city        = Column(String(100))
    state       = Column(String(100))
    country     = Column(String(100))
    postal_code = Column(String(20))
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="profile")


# -------------------------------------------------------------------
# FARMS
# -------------------------------------------------------------------

class Farm(Base):
    __tablename__ = "farms"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name        = Column(String(128), nullable=False)
    location    = Column(String(256))
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user    = relationship("User", back_populates="farms")
    devices = relationship("Device", back_populates="farm", cascade="all, delete-orphan")


# -------------------------------------------------------------------
# DEVICES & PROFILES
# -------------------------------------------------------------------

class Device(Base):
    __tablename__ = "devices"
    id = Column(String(64), primary_key=True, index=True, default=_uuid)
    user_id             = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    farm_id             = Column(Integer, ForeignKey("farms.id", ondelete="SET NULL"), nullable=True)
    mac_id              = Column(String(64), unique=True, nullable=False, index=True)
    name                = Column(String(128), nullable=False)
    type                = Column(Enum(DeviceType, name="device_type"), nullable=False)
    http_endpoint       = Column(String(256), nullable=False)
    location_description= Column(String(256))
    is_active           = Column(Boolean, nullable=False, default=True)
    last_seen           = Column(DateTime(timezone=True))
    firmware_version    = Column(String(32), nullable=False, server_default="0.0.0")
    created_at          = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at          = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # JSON blobs
    pump_configurations = Column(JSON)
    sensor_parameters   = Column(JSON)
    valve_configurations= Column(JSON)
    switch_configurations= Column(JSON)
    # relationships
    user               = relationship("User", back_populates="devices")
    farm               = relationship("Farm", back_populates="devices")
    dosing_profiles    = relationship("DosingProfile", back_populates="device", cascade="all, delete-orphan")
    sensor_readings    = relationship("SensorReading", back_populates="device", cascade="all, delete-orphan")
    dosing_operations  = relationship("DosingOperation", back_populates="device", cascade="all, delete-orphan")
    subscriptions      = relationship("Subscription", back_populates="device", cascade="all, delete-orphan")
    payment_orders     = relationship("PaymentOrder", back_populates="device", cascade="all, delete-orphan")
    tasks              = relationship("Task", back_populates="device", cascade="all, delete-orphan")


class DosingProfile(Base):
    __tablename__ = "dosing_profiles"

    id             = Column(Integer, primary_key=True, index=True)
    device_id      = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    plant_name     = Column(String(100), nullable=False)
    plant_type     = Column(String(100), nullable=False)
    growth_stage   = Column(String(50), nullable=False)
    seeding_date   = Column(DateTime(timezone=True), nullable=False)
    target_ph_min  = Column(Float, nullable=False)
    target_ph_max  = Column(Float, nullable=False)
    target_tds_min = Column(Float, nullable=False)
    target_tds_max = Column(Float, nullable=False)
    dosing_schedule= Column(JSON, nullable=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    device = relationship("Device", back_populates="dosing_profiles")

# app/models.py

class DeviceCommand(Base):
    __tablename__ = "device_commands"
    id            = Column(Integer, primary_key=True)
    device_id     = Column(String, index=True)
    action = Column(
        Enum("restart", "update", name="cmd_action", native_enum=False),
        nullable=False,
    )
    parameters    = Column(JSON, nullable=True)     # e.g. {"url": "..."}
    issued_at     = Column(DateTime, default=datetime.utcnow)
    dispatched    = Column(Boolean, default=False)

class Task(Base):
    __tablename__ = "tasks"

    id           = Column(Integer, primary_key=True, index=True)
    device_id= Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    type         = Column(String(50), nullable=False)
    parameters   = Column(JSON)
    status       = Column(String(50), nullable=False, default="pending")
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    device = relationship("Device", back_populates="tasks")


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id          = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    reading_type= Column(String(50), nullable=False)
    value       = Column(Float, nullable=False)
    timestamp   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    location    = Column(String(100))

    device = relationship("Device", back_populates="sensor_readings")


class DosingOperation(Base):
    __tablename__ = "dosing_operations"

    id          = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    operation_id= Column(String(100), unique=True, nullable=False)
    actions     = Column(JSON, nullable=False)
    status      = Column(String(50), nullable=False)
    timestamp   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    device = relationship("Device", back_populates="dosing_operations")


# -------------------------------------------------------------------
# PLANTS & ANALYSIS
# -------------------------------------------------------------------

class Plant(Base):
    __tablename__ = "plants"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String(100), nullable=False)
    type         = Column(String(100), nullable=False)
    growth_stage = Column(String(50),  nullable=False)
    seeding_date = Column(DateTime(timezone=True), nullable=False)
    region       = Column(String(100), nullable=False)
    location     = Column(String(100), nullable=False)
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SupplyChainAnalysis(Base):
    __tablename__ = "supply_chain_analysis"

    id                   = Column(Integer, primary_key=True, index=True)
    origin               = Column(String(100), nullable=False)
    destination          = Column(String(100), nullable=False)
    produce_type         = Column(String(50),  nullable=False)
    weight_kg            = Column(Float, nullable=False)
    transport_mode       = Column(String(50), server_default="railway", nullable=False)
    distance_km          = Column(Float, nullable=False)
    cost_per_kg          = Column(Float, nullable=False)
    total_cost           = Column(Float, nullable=False)
    estimated_time_hours = Column(Float, nullable=False)
    market_price_per_kg  = Column(Float, nullable=False)
    net_profit_per_kg    = Column(Float, nullable=False)
    final_recommendation = Column(String(200), nullable=False)
    created_at           = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at           = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    conversation_logs = relationship(
        "ConversationLog", back_populates="analysis", cascade="all, delete-orphan"
    )


class ConversationLog(Base):
    __tablename__ = "conversation_logs"

    id           = Column(Integer, primary_key=True, index=True)
    analysis_id  = Column(Integer, ForeignKey("supply_chain_analysis.id", ondelete="SET NULL"))
    conversation = Column(JSON, nullable=False)
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    analysis = relationship("SupplyChainAnalysis", back_populates="conversation_logs")


# -------------------------------------------------------------------
# SUBSCRIPTIONS & BILLING
# -------------------------------------------------------------------

class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(128), nullable=False)
    device_types  = Column(JSON, nullable=False)    # e.g. ["dosing_unit"]
    duration_days = Column(Integer, nullable=False)  # 28 to 730
    price_cents   = Column(Integer, nullable=False)
    created_by    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=False)
    created_at    = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    activation_keys = relationship("ActivationKey", back_populates="plan", cascade="all, delete-orphan")
    subscriptions   = relationship("Subscription", back_populates="plan", cascade="all, delete-orphan")
    payment_orders  = relationship("PaymentOrder", back_populates="plan", cascade="all, delete-orphan")


class ActivationKey(Base):
    __tablename__ = "activation_keys"

    id                  = Column(Integer, primary_key=True, index=True)
    key                 = Column(String(64), unique=True, nullable=False, index=True)
    device_type         = Column(Enum(DeviceType, name="activation_device_type"), nullable=False)
    plan_id             = Column(Integer, ForeignKey("subscription_plans.id", ondelete="CASCADE"), nullable=False)
    created_by          = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=False)
    created_at          = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    redeemed            = Column(Boolean, default=False, nullable=False)
    redeemed_at         = Column(DateTime(timezone=True), nullable=True)
    redeemed_device_id  = Column(String(64), ForeignKey("devices.id", ondelete="SET NULL"))
    redeemed_user_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    allowed_device_id   = Column(String(64), ForeignKey("devices.id", ondelete="SET NULL"))

    plan             = relationship("SubscriptionPlan", back_populates="activation_keys")
    creator          = relationship("User", foreign_keys=[created_by])
    redeemed_device  = relationship("Device", foreign_keys=[redeemed_device_id])
    redeemed_user    = relationship("User", foreign_keys=[redeemed_user_id])
    allowed_device   = relationship("Device", foreign_keys=[allowed_device_id], backref="allowed_activation_keys")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    plan_id    = Column(Integer, ForeignKey("subscription_plans.id", ondelete="SET NULL"), nullable=False)
    start_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    end_date   = Column(DateTime(timezone=True), nullable=False)
    active     = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user   = relationship("User", back_populates="subscriptions")
    device = relationship("Device", back_populates="subscriptions")
    plan   = relationship("SubscriptionPlan", back_populates="subscriptions")


class PaymentStatus(PyEnum):
    PENDING    = "pending"
    PROCESSING = "processing"
    COMPLETED  = "completed"
    FAILED     = "failed"


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id          = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"))
    plan_id            = Column(Integer, ForeignKey("subscription_plans.id", ondelete="SET NULL"), nullable=False)
    amount_cents       = Column(Integer, nullable=False)
    status             = Column(Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.PENDING, nullable=False)
    upi_transaction_id = Column(String(64))
    # 👇 NEW
    screenshot_path    = Column(String(256))
    expires_at         = Column(DateTime(timezone=True), nullable=False)
    created_at         = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at         = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user   = relationship("User", back_populates="payment_orders")
    device = relationship("Device", back_populates="payment_orders")
    plan   = relationship("SubscriptionPlan", back_populates="payment_orders")
# -------------------------------------------------------------------
# CAMERAS & DETECTIONS
# -------------------------------------------------------------------

class Camera(Base):
    __tablename__ = "cameras"

    id              = Column(String(64), primary_key=True, index=True)
    name            = Column(String(120), nullable=False)
    is_online       = Column(Boolean, default=False, nullable=False)
    last_seen       = Column(DateTime(timezone=True))
    frames_received = Column(Integer, default=0, nullable=False)
    clips_count     = Column(Integer, default=0, nullable=False)
    last_clip_time  = Column(DateTime(timezone=True))
    storage_used    = Column(Float, default=0.0, nullable=False)  # MB
    settings        = Column(JSON)
    hls_path        = Column(String(256), nullable=True)
    user_cameras     = relationship("UserCamera", back_populates="camera", cascade="all, delete-orphan")
    detection_records= relationship("DetectionRecord", back_populates="camera", cascade="all, delete-orphan")


class UserCamera(Base):
    __tablename__ = "user_cameras"

    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    camera_id = Column(String(64), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    nickname  = Column(String(120))

    user   = relationship("User", back_populates="cameras")
    camera = relationship("Camera", back_populates="user_cameras")


class DetectionRecord(Base):
    __tablename__ = "detection_records"

    id          = Column(Integer, primary_key=True, index=True)
    camera_id   = Column(String(64), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    object_name = Column(String(100), nullable=False)
    timestamp   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    camera = relationship("Camera", back_populates="detection_records")


class CloudKey(Base):
    __tablename__ = "cloud_keys"

    id         = Column(Integer, primary_key=True, index=True)
    key        = Column(String(64), unique=True, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("admins.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    creator = relationship("Admin", back_populates="cloud_keys")
    usages = relationship(
        "CloudKeyUsage",
        back_populates="cloud_key",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class CameraToken(Base):
    __tablename__ = "camera_tokens"
    camera_id = Column(String(64), primary_key=True)
    token     = Column(String(64), nullable=False)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())


class Admin(Base):
    __tablename__ = "admins"

    id           = Column(Integer, primary_key=True, index=True)
    email        = Column(String(128), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role         = Column(String(50), nullable=False, default="superadmin")
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    # 👇 **add this single line**
    cloud_keys = relationship(
        "CloudKey",
        back_populates="creator",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    

class ValveState(Base):
    __tablename__ = "valve_states"
    device_id  = Column(String(64), primary_key=True, index=True)
    states     = Column(JSON, nullable=False, default={})
    updated_at = Column(DateTime(timezone=True),
                        server_default=func.now(),
                        onupdate=func.now(),
                        nullable=False)
    
class SwitchState(Base):
    __tablename__ = "switch_states"
    device_id  = Column(String(64), primary_key=True, index=True)
    states     = Column(JSON, nullable=False, default={})
    updated_at = Column(DateTime(timezone=True),
                        server_default=func.now(),
                        onupdate=func.now(),
                        nullable=False)
    
class CloudKeyUsage(Base):
    __tablename__ = "cloud_key_usages"

    id           = Column(Integer, primary_key=True, index=True)
    cloud_key_id = Column(Integer, ForeignKey("cloud_keys.id", ondelete="CASCADE"), nullable=False)
    resource_id  = Column(String(64), nullable=False)   # device_id or camera_id
    used_at      = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    cloud_key = relationship("CloudKey", back_populates="usages")

# -------------------------------------------------------------------
# DEVICE TOKENS (generic)
# -------------------------------------------------------------------
class DeviceToken(Base):
    __tablename__ = "device_tokens"

    device_id   = Column(String(64), ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    token       = Column(String(64), unique=True, nullable=False, index=True)
    device_type = Column(Enum(DeviceType, name="token_device_type"), nullable=False)
    issued_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # 👇 NEW: tokens are valid 30 days by default
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc) + timedelta(days=30),
    )
    device = relationship("Device", lazy="joined")