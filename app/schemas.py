from enum import Enum
from typing import Any, Optional, List, Dict
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict, field_validator, EmailStr

# -------------------- Device Related Schemas -------------------- #

class DeviceType(str, Enum):
    DOSING_UNIT = "dosing_unit"
    PH_TDS_SENSOR = "ph_tds_sensor"
    ENVIRONMENT_SENSOR = "environment_sensor"
    VALVE_CONTROLLER = "valve_controller"
    SMART_SWITCH     = "smart_switch"
class PumpConfig(BaseModel):
    pump_number: int = Field(..., ge=1, le=4)
    chemical_name: str = Field(..., max_length=50)
    chemical_description: Optional[str] = Field(None, max_length=200)

    model_config = ConfigDict(from_attributes=True)

class ValveConfig(BaseModel):
    valve_id: int = Field(..., ge=1, le=4)
    name: Optional[str] = Field(None, max_length=50)

    model_config = ConfigDict(from_attributes=True)

class SwitchConfig(BaseModel):
    channel: int = Field(..., ge=1, le=8)
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
    id: str
    created_at: datetime
    updated_at: datetime
    is_active: bool
    last_seen: Optional[datetime] = None
    pump_configurations: Optional[List[PumpConfig]] = None
    sensor_parameters: Optional[Dict[str, str]] = None
    switch_configurations: Optional[List[SwitchConfig]] = None
    model_config = ConfigDict(from_attributes=True)

# -------------------- Dosing Related Schemas -------------------- #

class DosingAction(BaseModel):
    pump_number: int
    chemical_name: str
    dose_ml: float
    reasoning: str

class DosingProfileBase(BaseModel):
    device_id: str  
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
    device_id: str  
    operation_id: str
    actions: List[DosingAction]
    status: str
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)

class SensorReading(BaseModel):
    device_id: str
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

    model_config = ConfigDict(from_attributes=True)

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

class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    role: Optional[str] = None
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
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=256)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

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
    name: str = Field(..., max_length=128)
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

    model_config = ConfigDict(from_attributes=True)

class ValveDeviceCreate(DeviceBase):
    valve_configurations: List[ValveConfig] = Field(..., min_length=1, max_length=4)

    @field_validator('type')
    @classmethod
    def validate_device_type(cls, v):
        if v != DeviceType.VALVE_CONTROLLER:
            raise ValueError("Device type must be valve_controller for ValveDeviceCreate")
        return v
    
class UserProfileBase(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None

class UserProfileCreate(UserProfileBase):
    pass

class UserProfileResponse(UserProfileBase):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class UserResponse(BaseModel):
    id: int
    email: EmailStr
    role: str
    created_at: datetime
    profile: Optional[UserProfileResponse] = None

    model_config = ConfigDict(from_attributes=True)

class SubscriptionPlanCreate(BaseModel):
    name: str
    device_types: List[str]
    duration_days: int
    price_cents: int

class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    device_id: str
    plan_id: int
    start_date: datetime
    end_date: datetime
    active: bool

    model_config = ConfigDict(from_attributes=True)

class ActivationKeyResponse(BaseModel):
    activation_key: str

class SubscriptionPlanResponse(BaseModel):
    id: int
    name: str
    device_types: List[str]
    duration_days: int
    price_cents: int
    created_by: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class CreatePaymentRequest(BaseModel):
    device_id: str
    plan_id: int

class ConfirmPaymentRequest(BaseModel):
    upi_transaction_id: str = Field(..., max_length=64)

class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class PaymentOrderResponse(BaseModel):
    id: int
    user_id: int
    device_id: str
    plan_id: int
    amount_cents: int
    status: PaymentStatus
    upi_transaction_id: Optional[str]
    qr_code_url: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class DetectionRange(BaseModel):
    object_name: str
    start_time: datetime
    end_time: datetime

class CameraReportResponse(BaseModel):
    camera_id: str
    detections: List[DetectionRange]

class SwitchDeviceCreate(DeviceBase):
    switch_configurations: List[SwitchConfig] = Field(..., min_length=1, max_length=8)
    @field_validator('type')
    @classmethod
    def validate_device_type(cls, v):
        if v != DeviceType.SMART_SWITCH:
            raise ValueError("Device type must be smart_switch for SwitchDeviceCreate")
        return v
    

class PlantDosingResponse(BaseModel):
    plant_id: int
    actions: List[Dict[str,Any]]

class AuthResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

    model_config = ConfigDict(from_attributes=True)