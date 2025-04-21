# app/models.py

from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, JSON, Enum as SQLAlchemyEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime, timezone
from app.core.database import Base
from app.schemas import DeviceType

class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    farm_id = Column(Integer, ForeignKey("farms.id"), nullable=True)
    mac_id = Column(String(64), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    type = Column(SQLAlchemyEnum(DeviceType), nullable=False)
    http_endpoint = Column(String(256), nullable=False, unique=True)
    location_description = Column(String(256))
    is_active = Column(Boolean, default=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    farm = relationship("Farm", back_populates="devices")
    # JSON fields
    pump_configurations = Column(JSON, nullable=True)
    sensor_parameters = Column(JSON, nullable=True)
    valve_configurations = Column(JSON, nullable=True)
    # Relationships
    dosing_profiles = relationship("DosingProfile", back_populates="device", cascade="all, delete-orphan")
    sensor_readings = relationship("SensorReading", back_populates="device", cascade="all, delete-orphan")
    dosing_operations = relationship("DosingOperation", back_populates="device", cascade="all, delete-orphan")
    dosing_profiles = relationship("DosingProfile", back_populates="device", cascade="all, delete-orphan")
class DosingProfile(Base):
    __tablename__ = "dosing_profiles"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"))
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
    # Fix: Set updated_at with a default value so it is never None.
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    # Relationships
    device = relationship("Device", back_populates="dosing_profiles")

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True)
    type = Column(String)  # e.g., 'pump', 'reset'
    parameters = Column(JSON)
    status = Column(String, default="pending")  
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "type": self.type,
            **self.parameters
        }

class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"))
    reading_type = Column(String(50), nullable=False)
    value = Column(Float, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    device = relationship("Device", back_populates="sensor_readings")
    location = Column(String(100), nullable=True)
class DosingOperation(Base):
    __tablename__ = "dosing_operations"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"))
    operation_id = Column(String(100), unique=True, nullable=False)
    actions = Column(JSON, nullable=False)
    status = Column(String(50), nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)) 
    device = relationship("Device", back_populates="dosing_operations")

class Plant(Base):
    __tablename__ = "plants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    type = Column(String(100), nullable=False)
    growth_stage = Column(String(50), nullable=False)
    seeding_date = Column(DateTime(timezone=True), nullable=False)
    region = Column(String(100), nullable=False)
    location = Column(String(100), nullable = False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    
class SupplyChainAnalysis(Base):
    __tablename__ = "supply_chain_analysis"

    id = Column(Integer, primary_key=True, index=True)
    origin = Column(String(100), nullable=False)
    destination = Column(String(100), nullable=False)
    produce_type = Column(String(50), nullable=False)
    weight_kg = Column(Float, nullable=False)
    transport_mode = Column(String(50), default="railway")

    distance_km = Column(Float, nullable=False)
    cost_per_kg = Column(Float, nullable=False)
    total_cost = Column(Float, nullable=False)
    estimated_time_hours = Column(Float, nullable=False)

    market_price_per_kg = Column(Float, nullable=False)
    net_profit_per_kg = Column(Float, nullable=False)
    final_recommendation = Column(String(200), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

class ConversationLog(Base):
    __tablename__ = "conversation_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    analysis_id = Column(Integer, ForeignKey("supply_chain_analysis.id"), nullable=True)
    conversation = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(128), unique=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(50), nullable=False, default="user")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    farms = relationship("Farm", back_populates="user", cascade="all, delete-orphan")
    
    
class Farm(Base):
    __tablename__ = "farms"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(128), nullable=False)
    location = Column(String(256), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    devices = relationship("Device", back_populates="farm", cascade="all, delete-orphan")
    user = relationship("User", back_populates="farms")

class Camera(Base):
    __tablename__ = "cameras"

    id              = Column(String(64), primary_key=True, index=True)
    name            = Column(String(120), nullable=False)
    is_online       = Column(Boolean, default=False)
    last_seen       = Column(DateTime(timezone=True), nullable=True)
    frames_received = Column(Integer, default=0)
    clips_count     = Column(Integer, default=0)
    last_clip_time  = Column(DateTime(timezone=True), nullable=True)
    storage_used    = Column(Float, default=0.0)  # MB
    settings        = Column(JSON, nullable=True)

    users = relationship("UserCamera", back_populates="camera", cascade="all, delete-orphan")

class UserCamera(Base):
    __tablename__ = "user_cameras"

    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    camera_id = Column(String(64),  ForeignKey("cameras.id", ondelete="CASCADE"))
    nickname  = Column(String(120), nullable=True)

    user   = relationship("User", back_populates="cameras")
    camera = relationship("Camera", back_populates="users")

User.cameras = relationship("UserCamera", back_populates="user", cascade="all, delete-orphan")
