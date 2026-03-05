"""
test_agent.py
=============
Script de test local pour l'agent Concierge Touristique.
Simule un appel complet SANS toucher à Supabase (mode dry-run).

Usage :
    python test_agent.py
"""

import json
import os
from unittest.mock import MagicMock, patch

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Payload de test
# ---------------------------------------------------------------------------
TEST_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
TEST_COMPANY_ID = "hotel-azur-nice-test"

TEST_REQUEST = {
    "workspace_id": TEST_WORKSPACE_ID,
    "company_id": TEST_COMPANY_ID,
    "from_email": "marie.dupont@example.com",
    "subject": "Conseils pour notre séjour à Nice – famille avec 2 enfants",
    "body": """Bonjour,

Nous arrivons à Nice le 15 juillet pour 5 nuits avec nos deux enfants (8 et 11 ans).
Nous n'avons pas de voiture et préférons utiliser les transports en commun.
Notre budget est moyen. Nous aimons la culture, la plage et la gastronomie locale.
Nous souhaiterions aussi faire une excursion dans l'arrière-pays.

Pouvez-vous nous proposer un itinéraire et peut-être des idées de restaurants ?

Merci d'avance,
Marie Dupont""",
    "preferences": {
        "dates_sejour": "15 juillet → 20 juillet 2025",
        "type_voyageurs": "famille",
        "budget": "medium",
        "interets": "plage, culture, gastronomie, arrière-pays",
        "localisation": "Nice",
        "voiture": False,
        "nb_personnes": 4,
        "langue": "fr",
    },
}

# ---------------------------------------------------------------------------
# Exemple de réponse JSON attendue (pour documentation)
# ---------------------------------------------------------------------------
EXAMPLE_RESPONSE = {
    "reply": """Bonjour Madame Dupont,

Bienvenue sur la Côte d'Azur ! Voici un itinéraire pensé pour votre famille, sans voiture, pour profiter pleinement de Nice et de ses environs du 15 au 20 juillet.

---

## 🌅 Jour 1 – Arrivée & Vieux-Nice
**Matin** : Installez-vous et prenez vos marques. Promenade sur la **Promenade des Anglais** pour une première baignade sur les galets. Les enfants apprécieront les jeux d'eau près de la plage publique du **Beau Rivage**.
**Après-midi** : Explorez le **Vieux-Nice** (quartier Barroc) : ruelles colorées, glaces artisanales. Montez à la **Colline du Château** par l'ascenseur gratuit (côté Rauba Capeu) pour un panorama exceptionnel sur la Baie des Anges.
**Soir** : Dîner dans le Vieux-Nice. Essayez la **socca** (spécialité niçoise) dans une des nombreuses trattorias. Comptez 25-45€ par adulte en restaurant, moins en snacking.
**Transport** : Tram T2 depuis votre hôtel jusqu'au port, puis à pied.

## 🎨 Jour 2 – Culture & Cimiez
**Matin** : **Marché du Cours Saleya** (fleurs et produits locaux, jusqu'à ~13h). Idéal pour un pique-nique. Achetez pain bagnat et fruits.
**Après-midi** : **Musée Matisse** à Cimiez (bus ligne 15 ou 17). Tarif indicatif ~10€/adulte, gratuit -18 ans. Jardins du musée gratuits, parfaits pour les enfants.
**Soir** : Retour tram, dîner quartier Libération (plus local, moins touristique).
**Transport** : Bus lignes d'Azur, ticket ~1,70€/trajet ou pass journée recommandé.

## 🏖️ Jour 3 – Villefranche & Bord de mer
**Matin** : TER jusqu'à **Villefranche-sur-Mer** (10 min, ~4€ aller-retour). Belle plage de sable, village coloré avec sa citadelle. Les enfants adoreront.
**Après-midi** : Retour Nice, plage de l'après-midi puis glaces sur la Prom'.
**Transport** : TER depuis Gare Nice-Ville (fréquence élevée).

## 🌿 Jour 4 – Excursion arrière-pays : Èze & Saint-Paul-de-Vence
**Matin** : Bus ligne 82 depuis Nice → **Èze Village** perché (env. 30 min). Jardin Exotique (~7€/adulte), panorama sur Monaco. Prévoir chaussures confortables.
**Après-midi** : Taxi ou retour bus + correspondance vers **Saint-Paul-de-Vence** (galeries d'art, remparts médiévaux). Ambiance très différente, plus artistique.
**Soir** : Retour en bus/car ou taxi partagé. Dîner à Nice.
**Note** : Journée assez chargée, prévoir eau et snacks pour les enfants. La chaleur de juillet peut être intense.

## 🎉 Jour 5 – Libre & Shopping local
**Matin** : Marché de la Libération (plus authentique), produits locaux.
**Après-midi** : Libre selon envies – musée MAMAC (gratuit certains dimanches), shopping rue Masséna, ou farniente sur la plage.
**Soir** : Dîner d'adieu dans le Vieux-Nice.

---
*Ces suggestions sont indicatives. Les horaires et tarifs exacts sont à confirmer directement auprès des prestataires. En juillet, réservez les restaurants à l'avance, surtout le week-end.*""",

    "confidence": 0.88,
    "needs_human": False,

    "suggested_bookings": [
        {
            "type": "restaurant",
            "nom": "Restaurant dans le Vieux-Nice (à sélectionner)",
            "ville": "Nice",
            "date_souhaitee": "2025-07-15",
            "heure_souhaitee": "19:30",
            "nb_personnes": 4,
            "notes": "Famille avec 2 enfants (8 et 11 ans). Préférence cuisine niçoise traditionnelle. Budget moyen (~30-45€/adulte).",
            "email_draft": "Objet : Demande de réservation – Dîner du 15 juillet – 4 personnes\n\nBonjour,\n\nNous souhaiterions réserver une table pour 4 personnes (2 adultes, 2 enfants) le mardi 15 juillet 2025 à 19h30.\n\nNous venons d'arriver à Nice et cherchons à découvrir la cuisine niçoise traditionnelle.\n\nMerci de nous confirmer votre disponibilité.\n\nCordialement,\nMarie Dupont"
        },
        {
            "type": "excursion",
            "nom": "Visite Jardin Exotique d'Èze",
            "ville": "Èze",
            "date_souhaitee": "2025-07-18",
            "heure_souhaitee": "10:00",
            "nb_personnes": 4,
            "notes": "Billet d'entrée conseillé à l'avance en haute saison. Tarif indicatif ~7€/adulte, gratuit <12 ans (à vérifier). Accès bus ligne 82 depuis Nice.",
            "email_draft": None
        },
        {
            "type": "musee",
            "nom": "Musée Matisse – Cimiez",
            "ville": "Nice",
            "date_souhaitee": "2025-07-16",
            "heure_souhaitee": "14:00",
            "nb_personnes": 4,
            "notes": "Gratuit pour les moins de 18 ans. Tarif adulte indicatif ~10€. Fermé le mardi. Vérifier les horaires estivaux sur le site officiel.",
            "email_draft": None
        }
    ],

    "itinerary": [
        {
            "jour": 1,
            "date": "2025-07-15",
            "matin": "Arrivée, installation. Promenade des Anglais et baignade.",
            "apres_midi": "Vieux-Nice, Colline du Château (ascenseur gratuit).",
            "soiree": "Dîner Vieux-Nice, socca et cuisine niçoise.",
            "transport": "Tram T2 pour le Vieux-Nice (arrêt Garibaldi ou Opéra-Vieille-Ville)."
        },
        {
            "jour": 2,
            "date": "2025-07-16",
            "matin": "Marché Cours Saleya, pique-nique.",
            "apres_midi": "Musée Matisse à Cimiez.",
            "soiree": "Quartier Libération, dîner local.",
            "transport": "Bus lignes d'Azur. Pass journée recommandé (~5€)."
        },
        {
            "jour": 3,
            "date": "2025-07-17",
            "matin": "TER → Villefranche-sur-Mer, plage de sable.",
            "apres_midi": "Retour Nice, plage Promenade des Anglais.",
            "soiree": "Balade nocturne sur la Prom'.",
            "transport": "TER Nice-Ville → Villefranche (~10 min, ~2€ l'aller)."
        },
        {
            "jour": 4,
            "date": "2025-07-18",
            "matin": "Bus 82 → Èze Village, Jardin Exotique.",
            "apres_midi": "Saint-Paul-de-Vence, galeries, remparts.",
            "soiree": "Retour Nice, dîner.",
            "transport": "Bus ligne 82 pour Èze. Taxi ou correspondance pour Saint-Paul-de-Vence."
        },
        {
            "jour": 5,
            "date": "2025-07-20",
            "matin": "Marché Libération, shopping rue Masséna.",
            "apres_midi": "Libre. MAMAC ou plage.",
            "soiree": "Dîner d'adieu Vieux-Nice.",
            "transport": "Tram ou à pied selon hôtel."
        }
    ],

    "sources": [
        "https://www.lignesdazur.com",
        "https://www.nice.fr/fr/culture/musees-et-galeries",
        "https://www.eze-tourisme.com",
        "https://www.saint-pauldevence.com",
        "https://www.ter.sncf.com/sud-provence-alpes-cote-d-azur"
    ],

    "run_id": "550e8400-e29b-41d4-a716-446655440000",
    "credits_charged": 2,
    "processing_ms": 3241
}


# ---------------------------------------------------------------------------
# Test avec mock Supabase + OpenAI réel (si clé dispo)
# ---------------------------------------------------------------------------
def test_with_mocks():
    """Test complet avec Supabase mocké et OpenAI réel."""
    print("=" * 60)
    print("TEST CONCIERGE AGENT – Mode mock Supabase")
    print("=" * 60)

    import concierge_agent as agent

    # Mock Supabase
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "id": TEST_WORKSPACE_ID,
        "credits_remaining": 50,
        "subscription_status": "active",
        "plan": "starter",
    }

    with (
        patch("concierge_agent.get_supabase", return_value=mock_sb),
        patch("concierge_agent.fetch_workspace", return_value={"id": TEST_WORKSPACE_ID, "credits_remaining": 50, "subscription_status": "active"}),
        patch("concierge_agent.fetch_company", return_value={"company_id": TEST_COMPANY_ID, "workspace_id": TEST_WORKSPACE_ID, "knowledge_base": "Hôtel situé près de la gare de Nice. Petit-déjeuner servi 7h-10h. Parking disponible sur demande."}),
        patch("concierge_agent.fetch_agent_config", return_value={"id": agent.AGENT_ID, "price_per_run": 2, "tool_type": "prompt_direct", "prompt_system": None, "webhook_url": None}),
        patch("concierge_agent.fetch_agent_settings", return_value={"tone": "vous", "rules": {}}),
        patch("concierge_agent.insert_credit_usage"),
        patch("concierge_agent.upsert_agent_run"),
    ):
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if not openai_key or openai_key == "sk-...":
            print("\n⚠️  OPENAI_API_KEY non configurée → affichage de la réponse exemple\n")
            print(json.dumps(EXAMPLE_RESPONSE, ensure_ascii=False, indent=2))
            return

        print("\n🤖 Appel OpenAI réel en cours...\n")
        result = agent.run(
            workspace_id=TEST_WORKSPACE_ID,
            company_id=TEST_COMPANY_ID,
            from_email=TEST_REQUEST["from_email"],
            subject=TEST_REQUEST["subject"],
            body=TEST_REQUEST["body"],
            preferences=TEST_REQUEST["preferences"],
            supabase_url="https://mock.supabase.co",
            supabase_service_key="mock-key",
            openai_api_key=openai_key,
        )

        print(f"✅ Réponse reçue | confidence={result.get('confidence')} | {result.get('processing_ms')}ms")
        print(f"📋 {len(result.get('itinerary', []))} jours d'itinéraire")
        print(f"📅 {len(result.get('suggested_bookings', []))} pré-réservation(s) suggérée(s)")
        print("\n--- REPLY ---")
        print(result.get("reply", "")[:800] + "...")
        print("\n--- JSON COMPLET ---")
        print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])


if __name__ == "__main__":
    test_with_mocks()