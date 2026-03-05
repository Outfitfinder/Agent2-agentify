"""
concierge_agent.py
==================
Agent "Concierge Touristique" – Côte d'Azur (Nice / Cannes / Antibes / arrière-pays)
Compatible avec la plateforme Agentify (Supabase + OpenAI + Railway)

Modes supportés :
  - prompt_direct  → appel OpenAI GPT-4o
  - webhook_external → POST vers webhook_url tiers

Auteur  : Agentify Backend
Version : 1.0.0
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from openai import OpenAI
from supabase import Client, create_client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("concierge_agent")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(_handler)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
AGENT_ID = "concierge-touristique-cote-azur"
DEFAULT_MODEL = "gpt-4o"
FALLBACK_MODEL = "gpt-4o-mini"   # modèle moins cher pour prompts courts
MAX_TOKENS = 2048
TEMPERATURE = 0.4                 # bas = moins d'hallucinations

# Hachage pour cache itinéraires (clé = hash du contexte normalisé)
_ITINERARY_CACHE: dict[str, dict] = {}
CACHE_MAX_SIZE = 200


# ===========================================================================
# SYSTEM PROMPT (stocké aussi dans agents.prompt_system en DB)
# ===========================================================================
SYSTEM_PROMPT = """Tu es le concierge virtuel d'un hôtel ou d'une résidence sur la Côte d'Azur (Nice, Cannes, Antibes, Grasse, Èze, Saint-Paul-de-Vence, Valbonne et l'arrière-pays).

MISSION :
- Proposer des idées de sorties et des itinéraires personnalisés, jour par jour.
- Expliquer les transports locaux : TER Côte d'Azur, bus Lignes d'Azur, tram, taxi/VTC, parking (ne pas inventer de tarifs exacts).
- Proposer des pré-réservations via un brouillon de demande email structuré.
- Adapter les suggestions au profil (couple, famille, solo, senior, PMR).

RÈGLES ANTI-HALLUCINATION :
- Ne jamais inventer des horaires précis d'ouverture ou des prix exacts. Utilise des fourchettes (ex. : "environ 15-25 €", "généralement ouvert 10h-18h").
- Si tu n'es pas sûr d'une information, dis-le clairement et conseille de vérifier sur le site officiel.
- Ne pas inventer de noms de restaurants/hôtels inexistants.

FORMAT DE RÉPONSE (JSON strict, aucun texte hors du JSON) :
{
  "reply": "<texte complet, ton concierge professionnel, en français, avec markdown>",
  "confidence": <float 0.0-1.0>,
  "needs_human": <bool>,
  "suggested_bookings": [
    {
      "type": "<restaurant|musee|excursion|transport|autre>",
      "nom": "<nom du lieu>",
      "ville": "<ville>",
      "date_souhaitee": "<ISO date ou null>",
      "heure_souhaitee": "<HH:MM ou null>",
      "nb_personnes": <int ou null>,
      "notes": "<infos complémentaires>",
      "email_draft": "<brouillon email de demande de réservation, en français>"
    }
  ],
  "itinerary": [
    {
      "jour": <int>,
      "date": "<ISO date ou null>",
      "matin": "<description activités matin>",
      "apres_midi": "<description activités après-midi>",
      "soiree": "<description soirée>",
      "transport": "<conseils transport du jour>"
    }
  ],
  "sources": ["<url1>", "<url2>"]
}

TON : professionnel, chaleureux, concierge d'hôtel 4 étoiles. Vouvoiement systématique.
LANGUE : français uniquement sauf si le client écrit dans une autre langue.
"""


# ===========================================================================
# Helpers Supabase
# ===========================================================================

def get_supabase(url: str, service_key: str) -> Client:
    """Retourne un client Supabase avec la service role key (backend only)."""
    return create_client(url, service_key)


def fetch_agent_config(sb: Client, agent_id: str = AGENT_ID) -> dict:
    """Récupère la config de l'agent depuis la table agents."""
    resp = sb.table("agents").select("*").eq("id", agent_id).maybe_single().execute()
    if not resp.data:
        raise ValueError(f"Agent '{agent_id}' introuvable dans la table agents.")
    return resp.data


def fetch_workspace(sb: Client, workspace_id: str) -> dict:
    """Vérifie que le workspace existe et a des crédits."""
    resp = (
        sb.table("workspaces")
        .select("id,credits_remaining,subscription_status,plan")
        .eq("id", workspace_id)
        .maybe_single()
        .execute()
    )
    if not resp.data:
        raise ValueError(f"Workspace '{workspace_id}' introuvable.")
    ws = resp.data
    if ws["subscription_status"] != "active":
        raise PermissionError(f"Workspace '{workspace_id}' inactif ({ws['subscription_status']}).")
    if ws["credits_remaining"] <= 0:
        raise PermissionError(f"Workspace '{workspace_id}' : crédits insuffisants.")
    return ws


def fetch_company(sb: Client, company_id: str) -> dict:
    """Récupère les infos de l'établissement (email, knowledge_base…)."""
    resp = (
        sb.table("companies")
        .select("company_id,name,support_email,knowledge_base,workspace_id")
        .eq("company_id", company_id)
        .maybe_single()
        .execute()
    )
    if not resp.data:
        raise ValueError(f"Company '{company_id}' introuvable.")
    return resp.data


def fetch_agent_settings(sb: Client, workspace_id: str, agent_id: str = AGENT_ID) -> dict:
    """Récupère les réglages spécifiques workspace/agent (tone, rules…)."""
    resp = (
        sb.table("agent_settings")
        .select("*")
        .eq("workspace_id", workspace_id)
        .eq("agent_id", agent_id)
        .maybe_single()
        .execute()
    )
    return resp.data or {"tone": "vous", "rules": {}}


def insert_credit_usage(
    sb: Client,
    workspace_id: str,
    credits_used: int,
    action_type: str = "concierge_response",
) -> None:
    """
    Insère dans credit_usage → déclenche les triggers DB qui
    décrémentent workspaces.credits_remaining et vérifient > 0.
    """
    if not workspace_id:
        raise ValueError("workspace_id ne peut pas être NULL dans credit_usage.")
    sb.table("credit_usage").insert(
        {
            "id": str(uuid.uuid4()),
            "workspace_id": workspace_id,
            "credits_used": credits_used,
            "action_type": action_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()
    logger.info("💳 %d crédit(s) facturé(s) → workspace %s", credits_used, workspace_id)


def upsert_agent_run(
    sb: Client,
    run_id: str,
    workspace_id: str,
    input_text: str,
    output_text: str,
    status: str,
    processing_ms: int,
) -> None:
    """Insère/met à jour un enregistrement agent_runs pour le tracking."""
    try:
        sb.table("agent_runs").upsert(
            {
                "id": run_id,
                "workspace_id": workspace_id,
                "agent_id": AGENT_ID,
                "input_text": input_text[:4000],   # limite taille
                "output_text": output_text[:8000],
                "status": status,
                "minutes_saved_estimated": 15,      # 15 min = valeur estimée concierge
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except Exception as exc:
        logger.warning("agent_runs upsert failed (non-bloquant) : %s", exc)


# ===========================================================================
# Cache itinéraires
# ===========================================================================

def _cache_key(preferences: dict) -> str:
    """Génère une clé de cache déterministe depuis les préférences."""
    normalized = json.dumps(preferences, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _get_cached(key: str) -> dict | None:
    entry = _ITINERARY_CACHE.get(key)
    if entry:
        logger.info("♻️  Cache hit pour clé %s", key)
    return entry


def _set_cache(key: str, value: dict) -> None:
    if len(_ITINERARY_CACHE) >= CACHE_MAX_SIZE:
        # Supprime la plus ancienne entrée (FIFO simple)
        oldest = next(iter(_ITINERARY_CACHE))
        del _ITINERARY_CACHE[oldest]
    _ITINERARY_CACHE[key] = value


# ===========================================================================
# Construction du prompt utilisateur
# ===========================================================================

def build_user_prompt(
    body: str,
    preferences: dict,
    knowledge_base: str | None,
    settings: dict,
) -> str:
    """
    Assemble le prompt utilisateur de manière structurée pour réduire
    les tokens inutiles et guider le LLM efficacement.
    """
    parts: list[str] = []

    # --- Préférences structurées ---
    if preferences:
        pref_lines = []
        mapping = {
            "dates_sejour": "Dates du séjour",
            "type_voyageurs": "Type de voyageurs",
            "budget": "Budget",
            "interets": "Centres d'intérêt",
            "localisation": "Zone / localisation",
            "voiture": "Véhicule disponible",
            "accessibilite": "Besoins accessibilité",
            "nb_personnes": "Nombre de personnes",
            "langue": "Langue préférée",
        }
        for key, label in mapping.items():
            val = preferences.get(key)
            if val:
                pref_lines.append(f"- {label} : {val}")
        if pref_lines:
            parts.append("### Préférences du séjour\n" + "\n".join(pref_lines))

    # --- Message du client ---
    if body and body.strip():
        parts.append(f"### Message du client\n{body.strip()[:3000]}")

    # --- Knowledge base de l'établissement (résumé) ---
    if knowledge_base and knowledge_base.strip():
        # On tronque pour limiter les tokens
        kb_excerpt = knowledge_base.strip()[:1500]
        parts.append(
            f"### Informations spécifiques à l'établissement\n{kb_excerpt}"
        )

    # --- Ton personnalisé ---
    tone = settings.get("tone", "vous")
    rules = settings.get("rules", {})
    if tone or rules:
        tone_notes = [f"Utilise le vouvoiement : {'oui' if tone == 'vous' else 'non'}"]
        if rules:
            for k, v in rules.items():
                tone_notes.append(f"- {k} : {v}")
        parts.append("### Instructions spécifiques\n" + "\n".join(tone_notes))

    parts.append(
        "\nRéponds UNIQUEMENT en JSON valide selon le format défini. "
        "Aucun texte en dehors du JSON."
    )

    return "\n\n".join(parts)


# ===========================================================================
# Mode A : prompt_direct (OpenAI)
# ===========================================================================

def _call_openai(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Appelle l'API OpenAI et retourne le texte brut de la réponse.
    Fallback automatique sur gpt-4o-mini si gpt-4o échoue.
    """
    for attempt_model in [model, FALLBACK_MODEL]:
        try:
            response = client.chat.completions.create(
                model=attempt_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                response_format={"type": "json_object"},  # JSON mode OpenAI
            )
            content = response.choices[0].message.content
            logger.info("🤖 OpenAI %s → %d tokens", attempt_model, response.usage.total_tokens)
            return content
        except Exception as exc:
            logger.warning("OpenAI %s failed : %s – fallback…", attempt_model, exc)
            if attempt_model == FALLBACK_MODEL:
                raise
    raise RuntimeError("Tous les modèles OpenAI ont échoué.")


def run_prompt_direct(
    openai_client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    cache_key: str | None = None,
) -> dict:
    """Exécute le mode prompt_direct et retourne un dict parsé."""
    # Vérification cache
    if cache_key:
        cached = _get_cached(cache_key)
        if cached:
            return cached

    raw = _call_openai(openai_client, system_prompt, user_prompt)

    # Parsing robuste
    try:
        # Nettoie les éventuels backticks markdown
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        result = json.loads(clean.strip())
    except json.JSONDecodeError as exc:
        logger.error("Parsing JSON OpenAI failed : %s\nRaw: %s", exc, raw[:500])
        # Réponse dégradée structurée
        result = {
            "reply": raw,
            "confidence": 0.4,
            "needs_human": True,
            "suggested_bookings": [],
            "itinerary": [],
            "sources": [],
        }

    # Validation des champs obligatoires
    result.setdefault("confidence", 0.7)
    result.setdefault("needs_human", False)
    result.setdefault("suggested_bookings", [])
    result.setdefault("itinerary", [])
    result.setdefault("sources", [])

    # Mise en cache si fiable
    if cache_key and result.get("confidence", 0) >= 0.7:
        _set_cache(cache_key, result)

    return result


# ===========================================================================
# Mode B : webhook_external
# ===========================================================================

def run_webhook_external(
    webhook_url: str,
    payload: dict,
    timeout: int = 30,
) -> dict:
    """Envoie le payload au webhook tiers et retourne la réponse normalisée."""
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        logger.info("🌐 Webhook %s → HTTP %d", webhook_url, resp.status_code)
    except httpx.HTTPStatusError as exc:
        logger.error("Webhook HTTP error : %s", exc)
        raise
    except Exception as exc:
        logger.error("Webhook call failed : %s", exc)
        raise

    # Normalisation de la réponse webhook vers notre format interne
    return {
        "reply": data.get("reply") or data.get("message") or str(data),
        "confidence": float(data.get("confidence", 0.8)),
        "needs_human": bool(data.get("needs_human", False)),
        "suggested_bookings": data.get("suggested_bookings", []),
        "itinerary": data.get("itinerary", []),
        "sources": data.get("sources", []),
    }


# ===========================================================================
# Fonction principale : run()
# ===========================================================================

def run(
    *,
    # Identifiants multi-tenant (OBLIGATOIRES)
    workspace_id: str,
    company_id: str,
    # Contexte de la demande
    from_email: str,
    subject: str,
    body: str,
    # Préférences structurées (optionnel, complète le body)
    preferences: dict | None = None,
    # Config infra
    supabase_url: str,
    supabase_service_key: str,
    openai_api_key: str,
) -> dict:
    """
    Point d'entrée principal de l'agent Concierge Touristique.

    Paramètres
    ----------
    workspace_id         : UUID du workspace Agentify (obligatoire, jamais NULL)
    company_id           : ID de l'établissement (hôtel / résidence / chambre d'hôtes)
    from_email           : Email du client
    subject              : Sujet de la demande
    body                 : Corps du message client
    preferences          : Dict structuré (dates, type_voyageurs, budget, interets…)
    supabase_url         : URL Supabase
    supabase_service_key : Service role key (JAMAIS exposée côté front)
    openai_api_key       : Clé API OpenAI

    Retour
    ------
    dict avec : reply, confidence, needs_human, suggested_bookings,
                itinerary, sources, run_id, credits_charged
    """
    if not workspace_id:
        raise ValueError("workspace_id est obligatoire.")
    if not company_id:
        raise ValueError("company_id est obligatoire.")

    run_id = str(uuid.uuid4())
    start_ts = time.monotonic()
    preferences = preferences or {}

    logger.info(
        "🏨 Concierge agent démarré | run_id=%s | workspace=%s | company=%s",
        run_id, workspace_id, company_id,
    )

    # ------------------------------------------------------------------
    # 1. Initialisation clients
    # ------------------------------------------------------------------
    sb = get_supabase(supabase_url, supabase_service_key)
    openai_client = OpenAI(api_key=openai_api_key)

    # ------------------------------------------------------------------
    # 2. Vérifications DB
    # ------------------------------------------------------------------
    workspace = fetch_workspace(sb, workspace_id)
    company = fetch_company(sb, company_id)

    # Vérification cohérence workspace
    if company.get("workspace_id") and company["workspace_id"] != workspace_id:
        raise PermissionError(
            f"La company '{company_id}' n'appartient pas au workspace '{workspace_id}'."
        )

    agent_cfg = fetch_agent_config(sb, AGENT_ID)
    settings = fetch_agent_settings(sb, workspace_id, AGENT_ID)
    price_per_run: int = agent_cfg.get("price_per_run", 1)
    tool_type: str = agent_cfg.get("tool_type", "prompt_direct")
    system_prompt: str = agent_cfg.get("prompt_system") or SYSTEM_PROMPT
    webhook_url: str | None = agent_cfg.get("webhook_url")

    logger.info(
        "⚙️  Agent config | tool_type=%s | price=%d | credits_remaining=%d",
        tool_type, price_per_run, workspace["credits_remaining"],
    )

    # ------------------------------------------------------------------
    # 3. Construction du prompt
    # ------------------------------------------------------------------
    knowledge_base = company.get("knowledge_base")
    user_prompt = build_user_prompt(body, preferences, knowledge_base, settings)
    cache_key = _cache_key({**preferences, "subject": subject}) if preferences else None

    # ------------------------------------------------------------------
    # 4. Appel LLM ou Webhook
    # ------------------------------------------------------------------
    result: dict[str, Any]

    if tool_type == "webhook_external" and webhook_url:
        logger.info("📡 Mode webhook_external → %s", webhook_url)
        payload = {
            "run_id": run_id,
            "workspace_id": workspace_id,
            "company_id": company_id,
            "from_email": from_email,
            "subject": subject,
            "body": body,
            "preferences": preferences,
            "knowledge_base": knowledge_base,
        }
        result = run_webhook_external(webhook_url, payload)
    else:
        logger.info("🤖 Mode prompt_direct → OpenAI")
        result = run_prompt_direct(
            openai_client,
            system_prompt,
            user_prompt,
            cache_key=cache_key,
        )

    # ------------------------------------------------------------------
    # 5. Facturation crédits (APRÈS la réponse, avant le retour)
    # ------------------------------------------------------------------
    insert_credit_usage(
        sb,
        workspace_id=workspace_id,
        credits_used=price_per_run,
        action_type="concierge_response",
    )

    # ------------------------------------------------------------------
    # 6. Tracking agent_runs
    # ------------------------------------------------------------------
    elapsed_ms = int((time.monotonic() - start_ts) * 1000)
    upsert_agent_run(
        sb,
        run_id=run_id,
        workspace_id=workspace_id,
        input_text=f"[{from_email}] {subject}\n{body[:500]}",
        output_text=result.get("reply", "")[:2000],
        status="success",
        processing_ms=elapsed_ms,
    )

    # ------------------------------------------------------------------
    # 7. Réponse finale
    # ------------------------------------------------------------------
    result["run_id"] = run_id
    result["credits_charged"] = price_per_run
    result["processing_ms"] = elapsed_ms

    logger.info(
        "✅ Concierge run terminé | run_id=%s | confidence=%.2f | %dms | %d crédits",
        run_id, result.get("confidence", 0), elapsed_ms, price_per_run,
    )
    return result