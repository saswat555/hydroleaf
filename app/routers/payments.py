"""
Payment & subscription workflow

Flow
────
1.  **/create** (user)  
    • creates a `payment_orders` row in *pending* state  
    • returns a **shared** UPI QR pointing at our constant VPA

2.  **/confirm/{order_id}** (user)  
    • user submits `upi_transaction_id` → order moves to *processing*

3.  **/approve/{order_id}** (admin)  
    • protected by JWT → `get_current_admin`  
    • admin verifies off-chain and sets order to *completed*  
      which automatically activates the subscription

4.  (optional) **/reject/{order_id}** (admin) → *failed*
"""

from pathlib import Path
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.dependencies import get_current_admin, get_current_user
from app.models import PaymentOrder, PaymentStatus, Subscription, SubscriptionPlan
from app.schemas import (
    ConfirmPaymentRequest,
    CreatePaymentRequest,
    PaymentOrderResponse,
)

# ─────────────────────────────────────────────────────────────────────────────
# Routers
# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])

admin_router = APIRouter(
    prefix="/admin/payments",
    tags=["admin-payments"],
    dependencies=[Depends(get_current_admin)],          # <-- auth middleware
)

# include the admin routes in the generated docs
router.include_router(admin_router)

# ─────────────────────────────────────────────────────────────────────────────
# Globals & helpers
# ─────────────────────────────────────────────────────────────────────────────
QR_DIR         = Path("app/static/qr_codes")
STATIC_QR_FILE = QR_DIR / "hydroleaf_upi.png"
STATIC_QR_URL  = f"/static/qr_codes/{STATIC_QR_FILE.name}"
QR_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# User-side routes
# ─────────────────────────────────────────────────────────────────────────────
@router.post(
    "/create",
    response_model=PaymentOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_payment(
    req: CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    user = Depends(get_current_user),
):
    # 1) validate subscription plan
    plan = await db.get(SubscriptionPlan, req.plan_id)
    if not plan:
        raise HTTPException(404, "Subscription plan not found")

    # 2) persist order
    order = PaymentOrder(
        user_id      = user.id,
        device_id    = req.device_id,
        plan_id      = plan.id,
        amount_cents = plan.price_cents,
        expires_at   = datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    # 3) respond
    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = STATIC_QR_URL
    return resp


@router.post("/confirm/{order_id}", response_model=PaymentOrderResponse)
async def confirm_payment(
    order_id: int,
    req: ConfirmPaymentRequest,
    db:  AsyncSession = Depends(get_db),
    user = Depends(get_current_user),
):
    order = await db.get(PaymentOrder, order_id)

    if not order or order.user_id != user.id:
        raise HTTPException(404, "Payment order not found")
    if order.status != PaymentStatus.PENDING:
        raise HTTPException(400, "Order is not in PENDING state")
    if order.expires_at and order.expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "Order has expired – create a new one")

    order.upi_transaction_id = req.upi_transaction_id
    order.status             = PaymentStatus.PROCESSING
    await db.commit(); await db.refresh(order)

    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = STATIC_QR_URL
    return resp


@router.post("/upload/{order_id}", response_model=PaymentOrderResponse)
async def upload_screenshot(
    order_id: int,
    file: bytes = File(..., description="JPEG/PNG payment proof"),
    db:   AsyncSession = Depends(get_db),
    user = Depends(get_current_user),
):
    order = await db.get(PaymentOrder, order_id)

    if not order or order.user_id != user.id:
        raise HTTPException(404, "Order not found")
    if order.status != PaymentStatus.PENDING:
        raise HTTPException(400, "Cannot upload proof in current state")

    img_path = QR_DIR / f"proof_{order.id}.jpg"
    img_path.write_bytes(file)
    order.screenshot_path = str(img_path)
    await db.commit(); await db.refresh(order)
    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = STATIC_QR_URL
    return resp

# ─────────────────────────────────────────────────────────────────────────────
# Admin-side routes  (JWT → get_current_admin)
# ─────────────────────────────────────────────────────────────────────────────
@admin_router.get("/", response_model=list[PaymentOrderResponse])
async def list_orders(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PaymentOrder))).scalars().all()
    return [PaymentOrderResponse.from_orm(o) for o in rows]


@admin_router.post("/approve/{order_id}", response_model=PaymentOrderResponse)
async def approve_payment(
    order_id: int,
    db:   AsyncSession = Depends(get_db),
    _    = Depends(get_current_admin),      # explicit for clarity
):
    order = await db.get(PaymentOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if order.status != PaymentStatus.PROCESSING:
        raise HTTPException(400, "Order must be in PROCESSING state")

    # 1) complete the order
    order.status = PaymentStatus.COMPLETED

    # 2) activate subscription
    now  = datetime.now(timezone.utc)
    plan = await db.get(SubscriptionPlan, order.plan_id)
    sub  = Subscription(
        user_id    = order.user_id,
        device_id  = order.device_id,
        plan_id    = plan.id,
        start_date = now,
        end_date   = now + timedelta(days=plan.duration_days),
        active     = True,
    )
    db.add(sub)

    await db.commit(); await db.refresh(order)

    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = STATIC_QR_URL
    return resp


@admin_router.post("/reject/{order_id}", response_model=PaymentOrderResponse)
async def reject_payment(
    order_id: int,
    db:   AsyncSession = Depends(get_db),
    _    = Depends(get_current_admin),
):
    order = await db.get(PaymentOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if order.status not in (PaymentStatus.PENDING, PaymentStatus.PROCESSING):
        raise HTTPException(400, "Cannot reject in this state")

    order.status = PaymentStatus.FAILED
    await db.commit(); await db.refresh(order)
    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = STATIC_QR_URL
    return resp
