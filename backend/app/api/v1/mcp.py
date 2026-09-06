from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.workspace import Workspace
from app.models.report import Connector as ConnectorModel
from app.mcp.registry import MCPConnectorRegistry
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()
registry = MCPConnectorRegistry()

class ConnectorToggleResponse(BaseModel):
    id: str
    name: str
    provider: str
    status: str
    lastSyncedAt: str | None
    itemsSyncedCount: int
    is_future_connector: bool = False

@router.get("")
async def get_mcp_connectors(
    workspace_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not workspace_id:
        ws = db.query(Workspace).filter(Workspace.owner_id == current_user.id).first()
        if not ws:
            return []
        workspace_id = ws.id

    # Sync default registry connectors into DB if not present
    db_connectors = db.query(ConnectorModel).filter(ConnectorModel.workspace_id == workspace_id).all()
    
    registry_status = registry.list_all_connectors()
    
    # Reconcile DB with registry items
    for item in registry_status:
        if item.get("is_future_connector"):
            continue
        exists = any(c.connector_type == item["provider"] for c in db_connectors)
        if not exists:
            new_conn = ConnectorModel(
                workspace_id=workspace_id,
                name=item["name"],
                connector_type=item["provider"],
                status=item["status"],
                documents_synced=item["synced_count"]
            )
            db.add(new_conn)
            db.commit()
            
    # Re-fetch from DB
    db_connectors = db.query(ConnectorModel).filter(ConnectorModel.workspace_id == workspace_id).all()
    
    result = []
    for c in db_connectors:
        result.append({
            "id": c.id,
            "name": c.name,
            "provider": c.connector_type,
            "status": c.status,
            "lastSyncedAt": c.last_synced_at.strftime("%Y-%m-%d %H:%M") if c.last_synced_at else "Not synced",
            "itemsSyncedCount": c.documents_synced,
            "is_future_connector": False
        })
        
    # Append future connectors
    for item in registry_status:
        if item.get("is_future_connector"):
            result.append({
                "id": item["connector_id"],
                "name": item["name"],
                "provider": item["provider"],
                "status": item["status"],
                "lastSyncedAt": None,
                "itemsSyncedCount": 0,
                "is_future_connector": True
            })
            
    return result

@router.post("/{connector_id}/toggle", response_model=ConnectorToggleResponse)
async def toggle_mcp_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Find in DB
    conn = db.query(ConnectorModel).filter(ConnectorModel.id == connector_id).first()
    if not conn:
        # Check if it's a future connector
        registry_status = registry.list_all_connectors()
        match = next((item for item in registry_status if item.get("connector_id") == connector_id), None)
        if match:
            return ConnectorToggleResponse(
                id=connector_id,
                name=match["name"],
                provider=match["provider"],
                status="disconnected",
                lastSyncedAt=None,
                itemsSyncedCount=0,
                is_future_connector=True
            )
        raise HTTPException(status_code=404, detail="Connector not found")
        
    # Toggle status
    new_status = "disconnected" if conn.status == "connected" else "connected"
    conn.status = new_status
    conn.last_synced_at = datetime.utcnow()
    db.commit()
    db.refresh(conn)
    
    return ConnectorToggleResponse(
        id=conn.id,
        name=conn.name,
        provider=conn.connector_type,
        status=conn.status,
        lastSyncedAt=conn.last_synced_at.strftime("%Y-%m-%d %H:%M"),
        itemsSyncedCount=conn.documents_synced
    )
