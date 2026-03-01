#!/usr/bin/env python3
"""
FrenchBreaches Discord Notifier
Surveille l'API de FrenchBreaches et envoie 
des notifications via un Webhook Discord pour chaque nouveau leak détecté.
"""

import json
import requests
import sys
import time

from datetime import datetime, timezone
from dotenv import load_dotenv
from loguru import logger
from os import getenv
from pathlib import Path

# --- Configuration ---

load_dotenv()

# Loguru
logger.remove()
logger.add(
    sys.stderr,
    format="<level>{level.icon}</level> <cyan>{time:YYYY-MM-DD HH:mm:ss}</cyan> | <level>{message}</level>",
    level="INFO",
    colorize=True,
)

# FrenchBreaches
API_URL = getenv("API_URL")
BASE_URL = getenv("BASE_URL")
SEEN_FILE = Path(getenv("SEEN_FILE"))
HEADERS = {"User-Agent": getenv("USER_AGENT")}

# Discord
WEBHOOK_URL = getenv("DISCORD_WEBHOOK_URL")
EMBED_COLOR = int(getenv("DISCORD_EMBED_COLOR", "0xFF4444"), 16)
DISCORD_DESC_LIMIT = int(getenv("DISCORD_DESC_LIMIT", "4096"))
DISCORD_FIELD_LIMIT = int(getenv("DISCORD_FIELD_LIMIT", "1024"))

CHECK_INTERVAL = int(getenv("CHECK_INTERVAL", "600"))

# --- Persistance des leaks déjà vus ---

class LeakStorage:
    """Gère la persistance des IDs de leaks déjà notifiés via un fichier JSON."""

    def __init__(self, filepath: Path = SEEN_FILE):
        self.filepath = filepath

    def load(self) -> set[str]:
        """Charge les IDs de leaks déjà notifiés."""

        if not self.filepath.exists():
            return set()

        try:
            data = json.loads(self.filepath.read_text(encoding="utf-8"))
            return set(data)
        except (json.JSONDecodeError, IOError):
            return set()

    def save(self, seen_ids: set[str]) -> None:
        """
        Sauvegarde les IDs de leaks déjà notifiés.
        (Écrit dans un fichier temporaire avant de remplacer l'original)
        """

        tmp_path = self.filepath.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(sorted(seen_ids), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(self.filepath)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

# --- Client Discord (Webhook) ---

class DiscordNotifier:
    """Construit et envoie des embeds Discord via Webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    @staticmethod
    def _build_full_url(relative_path: str) -> str:
        """Transforme un chemin relatif en URL complète et nettoie les `/../`."""

        if not relative_path:
            return ""
        if relative_path.startswith("http"):
            return relative_path

        return f"{BASE_URL}{relative_path}".replace("/../", "/")

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Tronque un texte en ajoutant '…' s'il dépasse la limite."""
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def _build_embed(self, article: dict) -> dict:
        """Construit un embed Discord à partir des données d'un article."""

        title = article.get("title", "Inconnu").strip()
        description = self._truncate(
            article.get("description", "Aucune description disponible."),
            DISCORD_DESC_LIMIT,
        )
        date = article.get("date", "N/A")
        affected = article.get("affectedCount", 0)
        data_types = article.get("dataTypes", [])
        last_modified = article.get("lastModified", "")

        logo_url = self._build_full_url(article.get("logo", ""))
        header_url = self._build_full_url(article.get("headerImage", ""))

        # — Champs de l'embed —

        fields = [{
            "name": "📅 Date",
            "value": date,
            "inline": True
        }]

        if affected and affected > 0:
            fields.append({
                "name": "👥 Personnes affectées",
                "value": f"{affected:,}".replace(",", " "),
                "inline": True,
            })

        if data_types:
            fields.append({
                "name": "🔓 Données compromises",
                "value": self._truncate(", ".join(data_types), DISCORD_FIELD_LIMIT),
                "inline": False,
            })

        # — Assemblage de l'embed —

        embed = {
            "title": f"🚨 Nouveau Leak : {title}",
            "description": description,
            "color": EMBED_COLOR,
            "fields": fields,
            "footer": {"text": "FrenchBreaches Notifier • frenchbreaches.com"},
            "timestamp": last_modified or datetime.now(timezone.utc).isoformat(),
        }

        if logo_url:
            embed["thumbnail"] = {"url": logo_url}
        if header_url:
            embed["image"] = {"url": header_url}

        logger.debug(f"Embed construit : {embed}")
        return embed

    def send(self, article: dict) -> bool:
        """Envoie une notification Discord via le Webhook pour un article donné."""

        payload = {
            "username": "FrenchBreaches Notifier",
            "embeds": [self._build_embed(article)],
        }

        try:
            response = requests.post(self.webhook_url, json=payload, timeout=15)

            # Gestion du rate-limit Discord (code 429)
            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 5)
                logger.warning(f"Rate-limit Discord — attente de {retry_after}s…")
                time.sleep(retry_after)
                response = requests.post(self.webhook_url, json=payload, timeout=15)

            if response.status_code in (200, 204):
                logger.success(f"Notification envoyée : {article.get('title', 'N/A')}")
                return True

            logger.error(f"Discord a répondu {response.status_code} : {response.text}")
            return False

        except requests.RequestException as e:
            logger.error(f"Impossible d'envoyer la notification : {e}")
            return False


# --- Moniteur FrenchBreaches ---

class FrenchBreachesMonitor:
    """Orchestre la surveillance des leaks et l'envoi des notifications."""

    def __init__(self, notifier: DiscordNotifier, storage: LeakStorage):
        self.notifier = notifier
        self.storage = storage

    @staticmethod
    def _fetch_articles() -> list[dict]:
        """Récupère la liste des articles depuis l'API FrenchBreaches."""

        try:
            response = requests.get(API_URL, timeout=30, headers=HEADERS)
            response.raise_for_status()
            return response.json().get("articles", [])

        except requests.RequestException as e:
            logger.error(f"Impossible de contacter l'API : {e}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Réponse API invalide : {e}")

        return []

    def check_for_new_leaks(self) -> int:
        """
        Vérifie les nouveaux leaks et envoie les notifications.
        Retourne le nombre de nouvelles notifications envoyées.
        """

        articles = self._fetch_articles()
        if not articles:
            logger.info("Aucun article récupéré depuis l'API.")
            return 0

        seen_ids = self.storage.load()

        # Premier lancement : on enregistre tous les IDs sans notifier
        if not seen_ids:
            all_ids = {a["id"] for a in articles if "id" in a}
            self.storage.save(all_ids)
            logger.info(
                f"Premier lancement — {len(all_ids)} leaks existants enregistrés "
                f"(aucune notification envoyée)."
            )
            return 0

        new_articles = [a for a in articles if a.get("id") and a["id"] not in seen_ids]
        if not new_articles:
            logger.info(f"Aucun nouveau leak détecté. ({len(seen_ids)} leaks connus)")
            return 0

        logger.info(f"{len(new_articles)} nouveau(x) leak(s) détecté(s) !")
        sent = 0

        for article in new_articles:
            if self.notifier.send(article):
                seen_ids.add(article["id"])
                self.storage.save(seen_ids)
                sent += 1
                time.sleep(1)  # éviter le rate-limit

        return sent

    def run(self) -> None:
        """Lance la boucle de surveillance."""

        separator = "━" * 50
        logger.info(separator)
        logger.info("  FrenchBreaches Discord Notifier")
        logger.info(separator)
        logger.info(f"Webhook   : …{self.notifier.webhook_url[-20:]}")
        logger.info(f"Intervalle : {CHECK_INTERVAL}s")
        logger.info(separator)

        try:
            while True:
                self.check_for_new_leaks()
                logger.debug(f"Prochaine vérification dans {CHECK_INTERVAL}s…")
                time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Arrêt du notifier.")
            sys.exit(0)


# --- Main ---

def main():
    if not WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL non défini dans le fichier .env")
        logger.error("Copiez .env.example en .env et renseignez le webhook.")
        sys.exit(1)

    storage = LeakStorage()
    notifier = DiscordNotifier(WEBHOOK_URL)
    monitor = FrenchBreachesMonitor(notifier, storage)
    monitor.run()

if __name__ == "__main__":
    main()
