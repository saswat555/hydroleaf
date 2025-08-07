from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas import TransportRequest, SupplyChainAnalysisResponse
from app.core.database import get_db
from app.services.supply_chain_service import trigger_transport_analysis

router = APIRouter()

@router.post("/", response_model=SupplyChainAnalysisResponse)
async def analyze_supply_chain(request: TransportRequest, db: AsyncSession = Depends(get_db)):
    """
    Trigger the transport optimization analysis and return the results.
    """
    try:
        result = await trigger_transport_analysis(request.model_dump(), db)
        # result["analysis"] is the analysis record dictionary returned from our service
        return result["analysis"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
