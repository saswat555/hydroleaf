from pydantic import BaseModel, Field, ConfigDict, field_validator, EmailStr
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum

# -------------------- Device Related Schemas -------------------- #

class DeviceType(str, Enum):
    DOSING_UNIT = "dosing_unit"
    PH_TDS_SENSOR = "ph_tds_sensor"
    ENVIRONMENT_SENSOR = "environment_sensor"
    VALVE_CONTROLLER = "valve_controller"
   
class PumpConfig(BaseModel):
    pump_number: int = Field(..., ge=1, le=4)
    chemical_name: str = Field(..., max_length=50)
    chemical_description: Optional[str] = Field(None, max_length=200)

    model_config = ConfigDict(from_attributes=True)
class ValveConfig(BaseModel):
    valve_id: int = Field(..., ge=1, le=4)
    name: Optional[str] = Field(None, max_length=50)
    model_config = ConfigDict(from_attributes=True)
    
class DeviceBase(BaseModel):
    mac_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=128)
    type: DeviceType
    http_endpoint: str = Field(..., max_length=256)
    location_description: Optional[str] = Field(None, max_length=256)
    farm_id: Optional[int] = None
    model_config = ConfigDict(from_attributes=True)
    valve_configurations: Optional[List[ValveConfig]] = None

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

# -------------------- Dosing Related Schemas -------------------- #

class DosingAction(BaseModel):
    pump_number: int
    chemical_name: str
    dose_ml: float
    reasoning: str

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
    actions: List[DosingAction]
    status: str
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)

class SensorReading(BaseModel):
    device_id: int
    reading_type: str
    value: float
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)

# -------------------- Health Related Schemas -------------------- #

class HealthCheck(BaseModel):
    status: str
    version: str
    timestamp: datetime
    environment: str

class DatabaseHealthCheck(BaseModel):
    status: str
    type: str
    timestamp: datetime
    last_test: Optional[str]

class FullHealthCheck(BaseModel):
    system: HealthCheck
    database: DatabaseHealthCheck
    timestamp: datetime

class SimpleDosingCommand(BaseModel):
    pump: int = Field(..., ge=1, le=4, description="Pump number (1-4)")
    amount: float = Field(..., gt=0, description="Dose in milliliters")

# -------------------- Plant Related Schemas -------------------- #

class PlantBase(BaseModel):
    name: str = Field(..., max_length=100)
    type: str = Field(..., max_length=100)
    growth_stage: str = Field(..., max_length=50)
    seeding_date: datetime
    region: str = Field(..., max_length=100)
    location: str = Field(..., max_length=100)

class PlantCreate(PlantBase):
    """Schema for creating a new plant profile."""

class PlantResponse(PlantBase):
    """Schema for returning plant details."""
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

# -------------------- Supply Chain Related Schemas -------------------- #

class TransportRequest(BaseModel):
    origin: str
    destination: str
    produce_type: str
    weight_kg: float
    transport_mode: str = "railway"

class TransportCost(BaseModel):
    distance_km: float
    cost_per_kg: float
    total_cost: float
    estimated_time_hours: float

class SupplyChainAnalysisResponse(BaseModel):
    origin: str
    destination: str
    produce_type: str
    weight_kg: float
    transport_mode: str
    distance_km: float
    cost_per_kg: float
    total_cost: float
    estimated_time_hours: float
    market_price_per_kg: float
    net_profit_per_kg: float
    final_recommendation: str
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class CloudAuthenticationRequest(BaseModel):
    device_id: str
    cloud_key: str

class CloudAuthenticationResponse(BaseModel):
    token: str
    message: str

class DosingCancellationRequest(BaseModel):
    device_id: str
    event: str

# -------------------- User Related Schemas -------------------- #

class UserResponse(BaseModel):
    id: int
    email: EmailStr
    role: str
    created_at: datetime

    class Config:
        orm_mode = True

class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    # Production-level user updates include profile fields.
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=256)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)

class UserProfile(BaseModel):
    id: int
    email: EmailStr
    role: str
    first_name: str = Field(..., max_length=50)
    last_name: str = Field(..., max_length=50)
    phone: Optional[str] = Field(..., max_length=20)
    address: Optional[str] = Field(..., max_length=256)
    city: Optional[str] = Field(..., max_length=100)
    state: Optional[str] = Field(..., max_length=100)
    country: Optional[str] = Field(..., max_length=100)
    postal_code: Optional[str] = Field(..., max_length=20)
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
class UserCreate(BaseModel):
    email: EmailStr
    password: str
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=256)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    name: str = Field(..., max_length=128),  # ← extra comma makes this a tuple!
    location: Optional[str] = Field(None, max_length=256)


class FarmBase(BaseModel):
    name: str = Field(..., max_length=128)
    location: Optional[str] = Field(None, max_length=256)

class FarmCreate(FarmBase):
    pass

class FarmResponse(FarmBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class ValveDeviceCreate(DeviceBase):
    valve_configurations: List[ValveConfig] = Field(..., min_length=1, max_length=4)

    @field_validator('type')
    @classmethod
    def validate_device_type(cls, v):
        if v != DeviceType.VALVE_CONTROLLER:
            raise ValueError("Device type must be valve_controller for ValveDeviceCreate")
        return v