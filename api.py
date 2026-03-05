"""
api.py
======
API FastAPI pour l'agent Concierge Touristique.
Permet d'appeler l'agent via HTTP POST (intégration widget, app mobile, etc.)

Routes :
  POST /concierge/run        → exécute l'agent
  GET  /concierge/health     → health check
  POST /concierge/preferences → extraction structurée (sans LLM)

Sécurité :
  - Header X-Workspace-ID obligatoire
  - Bearer token (JWT Supabase) ou API key interne
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import concierge_agent as agent
from email_worker import extract_preferences_from_body

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
API_SECRET = os.getenv("INTERNAL_API_SECRET", "")  # optionnel : clé interne

app = FastAPI(
    title="Agentify – Concierge Touristique",
    version="1.0.0",
    description="Agent concierge Côte d'Azur pour hôtels indépendants",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas Pydantic
# ---------------------------------------------------------------------------

class Preferences(BaseModel):
    dates_sejour: str | None = Field(None, example="2025-07-10 → 2025-07-14")
    type_voyageurs: str | None = Field(None, example="couple")
    budget: str | None = Field(None, example="medium")
    interets: list[str] = Field(default_factory=list, example=["plage", "gastronomie"])
    localisation: str | None = Field(None, example="Nice")
    voiture: bool | None = None
    accessibilite: str | None = None
    nb_personnes: int | None = None
    langue: str | None = Field(None, example="fr")


class ConciergeRequest(BaseModel):
    company_id: str = Field(..., example="hotel-azur-nice")
    from_email: str = Field(..., example="client@example.com")
    subject: str = Field(default="Demande de conseils séjour")
    body: str = Field(..., example="Bonjour, nous arrivons samedi pour 4 nuits en famille...")
    preferences: Preferences | None = None


class HealthResponse(BaseModel):
    status: str
    agent_id: str
    version: str


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def require_workspace_id(x_workspace_id: str | None) -> str:
    if not x_workspace_id or not x_workspace_id.strip():
        raise HTTPException(
            status_code=400,
            detail="Header X-Workspace-ID obligatoire.",
        )
    return x_workspace_id.strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/concierge/health", response_model=HealthResponse, tags=["system"])
async def health():
    return {
        "status": "ok",
        "agent_id": agent.AGENT_ID,
        "version": "1.0.0",
    }


@app.post("/concierge/run", tags=["agent"])
async def run_concierge(
    request: ConciergeRequest,
    x_workspace_id: str | None = Header(None),
):
    """
    Exécute l'agent concierge pour une demande client.

    Headers requis :
      - X-Workspace-ID : UUID du workspace Agentify
    """
    workspace_id = require_workspace_id(x_workspace_id)

    # Convertit les préférences Pydantic en dict
    prefs_dict: dict[str, Any] = {}
    if request.preferences:
        raw = request.preferences.model_dump(exclude_none=True)
        if "interets" in raw and isinstance(raw["interets"], list):
            raw["interets"] = ", ".join(raw["interets"])
        prefs_dict = raw

    # Complète avec extraction heuristique si préférences partielles
    extracted = extract_preferences_from_body(request.body)
    for k, v in extracted.items():
        prefs_dict.setdefault(k, v)

    try:
        result = agent.run(
            workspace_id=workspace_id,
            company_id=request.company_id,
            from_email=request.from_email,
            subject=request.subject,
            body=request.body,
            preferences=prefs_dict,
            supabase_url=SUPABASE_URL,
            supabase_service_key=SUPABASE_SERVICE_KEY,
            openai_api_key=OPENAI_API_KEY,
        )
        return JSONResponse(content=result)

    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur interne : {exc}")


@app.post("/concierge/extract-preferences", tags=["utils"])
async def extract_preferences(body: dict):
    """
    Extrait les préférences structurées depuis un texte libre (sans LLM, gratuit).
    Utile pour pré-remplir un formulaire côté frontend.
    """
    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Champ 'text' requis.")
    return extract_preferences_from_body(text)


# ---------------------------------------------------------------------------
# Handler global d'erreurs
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Erreur serveur : {str(exc)[:200]}"},
    )