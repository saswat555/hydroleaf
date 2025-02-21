# app/models.py

from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, JSON, Enum as SQLAlchemyEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime, UTC
from app.core.database import Base
from app.schemas import DeviceType

class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    type = Column(SQLAlchemyEnum(DeviceType), nullable=False)
    mqtt_topic = Column(String(256), nullable=False, unique=True)
    location_description = Column(String(256))
    is_active = Column(Boolean, default=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # JSON fields
    pump_configurations = Column(JSON, nullable=True)
    sensor_parameters = Column(JSON, nullable=True)
    # Relationships
    dosing_profiles = relationship("DosingProfile", back_populates="device", cascade="all, delete-orphan")
    sensor_readings = relationship("SensorReading", back_populates="device", cascade="all, delete-orphan")
    dosing_operations = relationship("DosingOperation", back_populates="device", cascade="all, delete-orphan")

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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    device = relationship("Device", back_populates="dosing_profiles")

class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"))
    reading_type = Column(String(50), nullable=False)
    value = Column(Float, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    device = relationship("Device", back_populates="sensor_readings")

class DosingOperation(Base):
    __tablename__ = "dosing_operations"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"))
    operation_id = Column(String(100), unique=True, nullable=False)
    actions = Column(JSON, nullable=False)
    status = Column(String(50), nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    device = relationship("Device", back_populates="dosing_operations")