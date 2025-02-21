# app/schemas.py

from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum

class DeviceType(str, Enum):
    DOSING_UNIT = "dosing_unit"
    PH_TDS_SENSOR = "ph_tds_sensor"
    ENVIRONMENT_SENSOR = "environment_sensor"

class PumpConfig(BaseModel):
    pump_number: int = Field(..., ge=1, le=4)
    chemical_name: str = Field(..., max_length=50)
    chemical_description: Optional[str] = Field(None, max_length=200)

    model_config = ConfigDict(from_attributes=True)

class DeviceBase(BaseModel):
    name: str = Field(..., max_length=128)
    type: DeviceType
    mqtt_topic: str = Field(..., max_length=256)
    location_description: Optional[str] = Field(None, max_length=256)

    model_config = ConfigDict(from_attributes=True)

class DosingDeviceCreate(DeviceBase):
    pump_configurations: List[PumpConfig] = Field(..., min_length=1, max_length=4)
    
    @field_validator('type')
    @classmethod
    def validate_device_type(cls, v):
        if v != DeviceType.DOSING_UNIT:
            raise ValueError("Device type must be dosing_unit for DosingDeviceCreate")
        return v

class SensorDeviceCreate(DeviceBase):
    sensor_parameters: Dict[str, str] = Field(...)
    
    @field_validator('type')
    @classmethod
    def validate_device_type(cls, v):
        if v not in [DeviceType.PH_TDS_SENSOR, DeviceType.ENVIRONMENT_SENSOR]:
            raise ValueError("Device type must be a sensor type")
        return v

class DeviceResponse(DeviceBase):
    id: int
    created_at: datetime
    updated_at: datetime
    is_active: bool
    last_seen: Optional[datetime] = None
    pump_configurations: Optional[List[PumpConfig]] = None
    sensor_parameters: Optional[Dict[str, str]] = None

    model_config = ConfigDict(from_attributes=True)

class DosingProfileBase(BaseModel):
    device_id: int
    plant_name: str = Field(..., max_length=100)
    plant_type: str = Field(..., max_length=100)
    growth_stage: str = Field(..., max_length=50)
    seeding_date: datetime
    target_ph_min: float = Field(..., ge=0, le=14)
    target_ph_max: float = Field(..., ge=0, le=14)
    target_tds_min: float = Field(..., ge=0)
    target_tds_max: float = Field(..., ge=0)
    dosing_schedule: Dict[str, float] = Field(...)

    model_config = ConfigDict(from_attributes=True)

class DosingProfileCreate(DosingProfileBase):
    pass

class DosingProfileResponse(DosingProfileBase):
    id: int
    created_at: datetime
    updated_at: datetime

class DosingOperation(BaseModel):
    device_id: int
    operation_id: str
    actions: List[Dict[str, float]]
    status: str
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)

class SensorReading(BaseModel):
    device_id: int
    reading_type: str
    value: float
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)
    
class HealthCheck(BaseModel):
    status: str
    version: str
    timestamp: datetime
    environment: str

class MQTTHealthCheck(BaseModel):
    status: str
    broker: str
    port: int
    client_id: str
    timestamp: datetime
    topics: List[str]
    last_test: Optional[str]

class DatabaseHealthCheck(BaseModel):
    status: str
    type: str
    timestamp: datetime
    last_test: Optional[str]

class FullHealthCheck(BaseModel):
    system: HealthCheck
    mqtt: MQTTHealthCheck
    database: DatabaseHealthCheck
    timestamp: datetime