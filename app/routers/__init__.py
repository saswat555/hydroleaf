# app/routers/__init__.py

from .devices       import router as devices_router
from .dosing        import router as dosing_router
from .config        import router as config_router
from .plants        import router as plants_router
from .supply_chain  import router as supply_chain_router
from .farms         import router as farms_router
from .cloud         import router as cloud_router
from .auth          import router as auth_router
from .users         import router as users_router
from .admin_users   import router as admin_users_router
from .device_comm   import router as device_comm_router
from .admin         import router as admin_router
from .cameras       import router as cameras_router
# in routers/__init__.py
from .subscriptions import router as subscriptions_router
from .admin_subscriptions import router as admin_subscriptions_router

__all__ = [
    "devices_router", "dosing_router", "config_router", "plants_router",
    "supply_chain_router", "farms_router", "cloud_router", "auth_router",
    "users_router", "admin_users_router", "device_comm_router",
    "heartbeat_router", "admin_router", "cameras_router","subscriptions_router","admin_subscriptions_router"
]
