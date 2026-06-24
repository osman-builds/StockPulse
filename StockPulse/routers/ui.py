from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import get_db, get_inventory_items
from presentation import render_dashboard, render_landing_page, render_portal_page, render_supplier_dashboard

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def landing_page():
    return HTMLResponse(render_landing_page())


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(db: Session = Depends(get_db)):
    return HTMLResponse(render_dashboard(get_inventory_items(db)))


@router.get("/user", response_class=HTMLResponse)
def user_page():
    return HTMLResponse(render_portal_page("user"))


@router.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(render_portal_page("admin"))


@router.get("/supplier", response_class=HTMLResponse)
def supplier_page():
    return HTMLResponse(render_supplier_dashboard())
