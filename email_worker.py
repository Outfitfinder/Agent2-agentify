"""
email_worker.py
===============
Worker email pour l'agent Concierge Touristique.
Pattern identique au bot existant en prod :
  1. Lit emails Gmail via IMAP
  2. Crée un ticket Supabase
  3. Appelle concierge_agent.run()
  4. Crée un brouillon Gmail
  5. Met à jour le ticket
  6. La facturation est gérée dans concierge_agent.run()

Usage :
    python email_worker.py          # boucle continue
    python email_worker.py --once   # un seul passage (cron)
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import logging
import os
import re
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from supabase import create_client

import concierge_agent as agent

load_dotenv()

logger = logging.getLogger("email_worker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(h)

# ---------------------------------------------------------------------------
# Config (variables d'environnement)
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Helpers email
# ---------------------------------------------------------------------------

def _clean_header(value: str) -> str:
    """Supprime les \n et \r interdits dans les headers email."""
    return re.sub(r"[\r\n]+", " ", value).strip()


def _extract_body(msg: email.message.Message) -> str:
    """Extrait le texte brut du message email."""
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body_parts.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(body_parts).strip()


# ---------------------------------------------------------------------------
# IMAP : lecture emails non lus
# ---------------------------------------------------------------------------

def fetch_unread_emails(
    imap_host: str,
    email_address: str,
    app_password: str,
    mailbox: str = "INBOX",
) -> list[dict]:
    """Retourne la liste des emails non lus sous forme de dicts."""
    emails = []
    try:
        mail = imaplib.IMAP4_SSL(imap_host)
        mail.login(email_address, app_password)
        mail.select(mailbox)
        _, uids = mail.search(None, "UNSEEN")
        for uid in uids[0].split():
            _, data = mail.fetch(uid, "(RFC822)")
            raw = data[0][1]
            msg = email.message_from_bytes(raw)
            emails.append(
                {
                    "uid": uid.decode(),
                    "message_id": msg.get("Message-ID", f"<{uid.decode()}@local>"),
                    "from_email": msg.get("From", ""),
                    "subject": msg.get("Subject", "(Sans objet)"),
                    "body": _extract_body(msg),
                }
            )
            # Marque comme lu
            mail.store(uid, "+FLAGS", "\\Seen")
        mail.logout()
    except Exception as exc:
        logger.error("IMAP error : %s", exc)
    return emails


# ---------------------------------------------------------------------------
# Supabase : gestion tickets
# ---------------------------------------------------------------------------

def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def ticket_exists(sb, message_id: str) -> bool:
    """Vérifie l'anti-doublon via message_id."""
    resp = (
        sb.table("tickets")
        .select("id")
        .eq("message_id", message_id)
        .maybe_single()
        .execute()
    )
    return resp.data is not None


def create_ticket(
    sb,
    *,
    workspace_id: str,
    company_id: str,
    message_id: str,
    from_email: str,
    subject: str,
    body: str,
) -> int:
    """Crée un ticket initial avec status='new'. Retourne l'id."""
    assert workspace_id, "workspace_id obligatoire pour tickets"
    resp = (
        sb.table("tickets")
        .insert(
            {
                "message_id": _clean_header(message_id),
                "company_id": company_id,
                "workspace_id": workspace_id,
                "from_email": from_email[:255],
                "subject": _clean_header(subject)[:500],
                "body": body[:10000],
                "status": "new",
                "confidence": 0.0,
                "draft_created": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .execute()
    )
    ticket_id = resp.data[0]["id"]
    logger.info("🎫 Ticket #%d créé | message_id=%s", ticket_id, message_id)
    return ticket_id


def update_ticket(
    sb,
    ticket_id: int,
    *,
    status: str,
    reply: str,
    confidence: float,
    draft_created: int,
    processing_ms: int,
    reason: str = "",
) -> None:
    sb.table("tickets").update(
        {
            "status": status,
            "reply": reply[:10000],
            "confidence": round(confidence, 4),
            "draft_created": draft_created,
            "processing_ms": processing_ms,
            "reason": reason[:500],
        }
    ).eq("id", ticket_id).execute()
    logger.info("🎫 Ticket #%d mis à jour → status=%s", ticket_id, status)


# ---------------------------------------------------------------------------
# Gmail : création brouillon
# ---------------------------------------------------------------------------

def create_gmail_draft(
    smtp_host: str,
    email_address: str,
    app_password: str,
    *,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> bool:
    """
    Crée un brouillon Gmail via SMTP (DRAFTS folder).
    Retourne True si succès.
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = _clean_header(subject)
        msg["From"] = email_address
        msg["To"] = _clean_header(to_email)
        msg["X-Agent"] = "Agentify-Concierge/1.0"

        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        with smtplib.SMTP_SSL(smtp_host, 465) as smtp:
            smtp.login(email_address, app_password)
            # Sauvegarde dans [Gmail]/Drafts
            smtp.sendmail(email_address, [email_address], msg.as_string())
            # Note : pour un vrai brouillon, utiliser IMAP APPEND sur [Gmail]/Drafts
        return True
    except Exception as exc:
        logger.error("Gmail draft error : %s", exc)
        return False


def create_gmail_draft_imap(
    imap_host: str,
    email_address: str,
    app_password: str,
    *,
    to_email: str,
    subject: str,
    body_text: str,
) -> bool:
    """Crée un vrai brouillon Gmail via IMAP APPEND (méthode recommandée)."""
    try:
        msg = MIMEMultipart()
        msg["Subject"] = _clean_header(subject)
        msg["From"] = email_address
        msg["To"] = _clean_header(to_email)
        msg["X-Agent"] = "Agentify-Concierge/1.0"
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

        mail = imaplib.IMAP4_SSL(imap_host)
        mail.login(email_address, app_password)
        # Dossier brouillons Gmail
        mail.append(
            "[Gmail]/Drafts",
            "\\Draft",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        mail.logout()
        logger.info("📝 Brouillon Gmail créé → %s", to_email)
        return True
    except Exception as exc:
        logger.error("Gmail IMAP draft error : %s", exc)
        return False


# ---------------------------------------------------------------------------
# Extraction préférences depuis le body (heuristique légère)
# ---------------------------------------------------------------------------

def extract_preferences_from_body(body: str) -> dict:
    """
    Tente d'extraire des champs structurés depuis le texte libre.
    Fallback : retourne dict vide (le LLM comprendra de toute façon).
    """
    prefs: dict = {}
    body_lower = body.lower()

    # Budget
    if any(w in body_lower for w in ["petit budget", "économique", "pas cher"]):
        prefs["budget"] = "low"
    elif any(w in body_lower for w in ["luxe", "haut de gamme", "premium"]):
        prefs["budget"] = "high"
    elif any(w in body_lower for w in ["budget moyen", "raisonnable"]):
        prefs["budget"] = "medium"

    # Type voyageurs
    if any(w in body_lower for w in ["famille", "enfant", "enfants", "kids"]):
        prefs["type_voyageurs"] = "famille"
    elif any(w in body_lower for w in ["couple", "romantique", "amoureux"]):
        prefs["type_voyageurs"] = "couple"
    elif any(w in body_lower for w in ["solo", "seul", "seule"]):
        prefs["type_voyageurs"] = "solo"

    # Voiture
    if any(w in body_lower for w in ["sans voiture", "pas de voiture", "transports en commun"]):
        prefs["voiture"] = False
    elif any(w in body_lower for w in ["avec voiture", "en voiture", "loue une voiture"]):
        prefs["voiture"] = True

    return prefs


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------

def process_company(sb, company: dict) -> None:
    """Traite les emails non lus pour une company."""
    company_id = company["company_id"]
    workspace_id = company["workspace_id"]
    support_email = company["support_email"]
    app_password = company["app_password"]

    if not workspace_id:
        logger.error("Company %s : workspace_id NULL → skip", company_id)
        return

    unread = fetch_unread_emails(
        imap_host="imap.gmail.com",
        email_address=support_email,
        app_password=app_password,
    )

    if not unread:
        return

    logger.info("📬 %d email(s) non lus → company %s", len(unread), company_id)

    for mail_data in unread:
        message_id = mail_data["message_id"]

        # Anti-doublon
        if ticket_exists(sb, message_id):
            logger.info("⏩ Doublon ignoré : %s", message_id)
            continue

        ticket_id = create_ticket(
            sb,
            workspace_id=workspace_id,
            company_id=company_id,
            message_id=message_id,
            from_email=mail_data["from_email"],
            subject=mail_data["subject"],
            body=mail_data["body"],
        )

        preferences = extract_preferences_from_body(mail_data["body"])

        try:
            result = agent.run(
                workspace_id=workspace_id,
                company_id=company_id,
                from_email=mail_data["from_email"],
                subject=mail_data["subject"],
                body=mail_data["body"],
                preferences=preferences,
                supabase_url=SUPABASE_URL,
                supabase_service_key=SUPABASE_SERVICE_KEY,
                openai_api_key=OPENAI_API_KEY,
            )

            reply_text = result.get("reply", "")
            confidence = result.get("confidence", 0.0)
            needs_human = result.get("needs_human", False)
            status = "needs_review" if needs_human or confidence < 0.6 else "answered"

            # Enrichir la réponse avec les bookings suggérés
            bookings = result.get("suggested_bookings", [])
            if bookings:
                reply_text += "\n\n---\n**Pré-réservations suggérées :**\n"
                for b in bookings:
                    reply_text += (
                        f"\n- {b.get('type', '').upper()} : {b.get('nom', '')} "
                        f"({b.get('ville', '')}) – {b.get('notes', '')}"
                    )

            # Brouillon Gmail
            draft_ok = create_gmail_draft_imap(
                imap_host="imap.gmail.com",
                email_address=support_email,
                app_password=app_password,
                to_email=mail_data["from_email"],
                subject=f"Re: {mail_data['subject']}",
                body_text=reply_text,
            )

            update_ticket(
                sb,
                ticket_id,
                status=status,
                reply=reply_text,
                confidence=confidence,
                draft_created=1 if draft_ok else 0,
                processing_ms=result.get("processing_ms", 0),
                reason="needs_human" if needs_human else "",
            )

        except PermissionError as exc:
            logger.warning("❌ Accès refusé : %s", exc)
            update_ticket(
                sb, ticket_id,
                status="failed", reply="", confidence=0.0,
                draft_created=0, processing_ms=0, reason=str(exc),
            )
        except Exception as exc:
            logger.error("❌ Erreur traitement ticket #%d : %s", ticket_id, exc)
            update_ticket(
                sb, ticket_id,
                status="failed", reply="", confidence=0.0,
                draft_created=0, processing_ms=0, reason=str(exc)[:500],
            )


def run_loop(once: bool = False) -> None:
    """Boucle principale du worker."""
    sb = get_supabase()
    logger.info("🚀 Email worker démarré (once=%s, interval=%ds)", once, POLL_INTERVAL)

    while True:
        try:
            # Récupère toutes les companies actives ayant une app_password configurée
            resp = (
                sb.table("companies")
                .select("company_id,name,support_email,app_password,knowledge_base,workspace_id")
                .not_.is_("app_password", "null")
                .not_.is_("workspace_id", "null")
                .execute()
            )
            companies = resp.data or []
            logger.info("🏨 %d établissement(s) actif(s)", len(companies))

            for company in companies:
                process_company(sb, company)

        except Exception as exc:
            logger.error("Worker loop error : %s", exc)

        if once:
            break
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Concierge Email Worker")
    parser.add_argument("--once", action="store_true", help="Un seul passage puis exit")
    args = parser.parse_args()
    run_loop(once=args.once)