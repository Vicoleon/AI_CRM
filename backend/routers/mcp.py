import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.database.db import get_db
from rubik_core.database.models import MCPConnection

router = APIRouter(prefix="/mcp", tags=["mcp"])


class MCPConnectionCreate(BaseModel):
    name: str
    kind: str
    command: str | None = None
    args: list | None = None
    server_url: str | None = None
    auth_mode: str = "iam"
    service_account_ref: str | None = None
    allowed_tools: list = []
    read_only: bool = True
    enabled: bool = True
    environment: str = "dev"


class MCPConnectionResponse(MCPConnectionCreate):
    id: str

    class Config:
        from_attributes = True


@router.get("/", response_model=list[MCPConnectionResponse])
async def list_mcp_connections(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """List all registered MCP connections (Admin only)."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    connections = db.query(MCPConnection).all()
    return connections


@router.post("/", response_model=MCPConnectionResponse)
async def create_mcp_connection(
    data: MCPConnectionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a new MCP connection (Admin only)."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    existing = db.query(MCPConnection).filter(MCPConnection.name == data.name).first()
    if existing:
        raise HTTPException(
            status_code=400, detail="Connection with this name already exists"
        )

    conn = MCPConnection(id=str(uuid.uuid4()), **data.dict())
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@router.delete("/{conn_id}")
async def delete_mcp_connection(
    conn_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Delete an MCP connection (Admin only)."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    conn = db.query(MCPConnection).filter(MCPConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    db.delete(conn)
    db.commit()
    return {"status": "deleted"}
