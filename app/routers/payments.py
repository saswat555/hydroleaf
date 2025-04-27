# app/routers/payments.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.models import PaymentOrder, SubscriptionPlan, Subscription, PaymentStatus
from app.schemas import (
    CreatePaymentRequest,
    ConfirmPaymentRequest,
    PaymentOrderResponse
)
import segno
from pathlib import Path
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])
admin_router = APIRouter(
    prefix="/admin/payments",
    tags=["admin-payments"],
    dependencies=[Depends(get_current_admin)]
)

# Where to save QR images
QR_DIR = Path("app/static/qr_codes")
QR_DIR.mkdir(parents=True, exist_ok=True)

@router.post("/create", response_model=PaymentOrderResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    req: CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    user = Depends(get_current_user),
):
    # 1) Validate plan & device
    plan = await db.get(SubscriptionPlan, req.plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    # you could also verify device belongs to user if needed

    # 2) Create the order
    order = PaymentOrder(
        user_id      = user.id,
        device_id    = req.device_id,
        plan_id      = plan.id,
        amount_cents = plan.price_cents,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    # 3) Generate UPI URL & QR
    upi_url = (
        f"upi://pay?"
        f"pa=your-upi-id@bank&"
        f"pn=Hydroleaf&"
        f"am={order.amount_cents/100:.2f}&"
        f"cu=INR&"
        f"tn={order.id}"
    )
    qr = segno.make(upi_url)
    qr_file = QR_DIR / f"order_{order.id}.png"
    qr.save(str(qr_file), scale=5, border=1)

    # 4) Respond with the QR code URL
    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = f"/static/qr_codes/order_{order.id}.png"
    return resp

@router.post("/confirm/{order_id}", response_model=PaymentOrderResponse)
async def confirm_payment(
    order_id: int,
    req: ConfirmPaymentRequest,
    db: AsyncSession = Depends(get_db),
    user = Depends(get_current_user),
):
    order = await db.get(PaymentOrder, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(404, "Order not found")
    if order.status != PaymentStatus.PENDING:
        raise HTTPException(400, "Cannot confirm order in its current status")

    order.upi_transaction_id = req.upi_transaction_id
    order.status             = PaymentStatus.PROCESSING
    await db.commit()
    await db.refresh(order)
    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = f"/static/qr_codes/order_{order.id}.png"
    return resp

@admin_router.get("/", response_model=list[PaymentOrderResponse])
async def list_orders(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PaymentOrder))
    orders = result.scalars().all()
    return [PaymentOrderResponse.from_orm(o) for o in orders]

@admin_router.post("/approve/{order_id}", response_model=PaymentOrderResponse)
async def approve_payment(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    order = await db.get(PaymentOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if order.status != PaymentStatus.PROCESSING:
        raise HTTPException(400, "Order not ready for approval")

    # 1) Mark completed
    order.status = PaymentStatus.COMPLETED

    # 2) Create the subscription
    now = datetime.utcnow()
    plan = await db.get(SubscriptionPlan, order.plan_id)
    sub = Subscription(
        user_id    = order.user_id,
        device_id  = order.device_id,
        plan_id    = plan.id,
        start_date = now,
        end_date   = now + timedelta(days=plan.duration_days),
        active     = True
    )
    db.add(sub)

    await db.commit()
    await db.refresh(order)

    resp = PaymentOrderResponse.from_orm(order)
    resp.qr_code_url = f"/static/qr_codes/order_{order.id}.png"
    return resp
