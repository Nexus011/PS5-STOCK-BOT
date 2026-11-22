"""Surveillance de fiches PS5 publiques et alertes Telegram, sans dépendances."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.json"
STATE_PATH = ROOT / "state.json"
USER_AGENT = "Mozilla/5.0 (compatible; PS5StockWatch/1.0; +https://core.telegram.org/bots)"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


class PageParser(HTMLParser):
    """Collect structured product data and stable metadata from a product page."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.jsonld: list[Any] = []
        self._script_type = ""
        self._script_chunks: list[str] = []
        self._in_script = False
        self._in_title = False
        self.title_chunks: list[str] = []
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and a.get("content"):
                self.meta[key.lower()] = a["content"]
        elif tag == "script":
            self._script_type = a.get("type", "").lower()
            self._script_chunks = []
            self._in_script = True
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if "ld+json" in self._script_type:
                raw = "".join(self._script_chunks).strip()
                if raw:
                    try:
                        self.jsonld.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
            self._script_type = ""
            self._script_chunks = []
            self._in_script = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            if "ld+json" in self._script_type:
                self._script_chunks.append(data)
        else:
            clean = " ".join(data.split())
            if clean:
                self.text_chunks.append(clean)
                if self._in_title:
                    self.title_chunks.append(clean)


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def price_number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("price") or value.get("lowPrice")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.search(r"\d[\d\s.,]*", value.replace("\u202f", " "))
    if not match:
        return None
    raw = re.sub(r"\s", "", match.group())
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def fetch(url: str, timeout: int) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, response.read(4_000_000).decode(charset, errors="replace"), response.geturl()
    except urllib.error.HTTPError as exc:
        body = exc.read(100_000).decode("utf-8", errors="replace")
        return exc.code, body, url


def inspect_page(source: dict[str, Any], timeout: int, max_price: float | None) -> dict[str, Any]:
    status, body, final_url = fetch(source["url"], timeout)
    if status in (403, 429, 503):
        return {"state": "blocked", "detail": f"HTTP {status} — vérifier le site manuellement"}
    if status < 200 or status >= 300:
        return {"state": "error", "detail": f"HTTP {status}"}

    parser = PageParser()
    parser.feed(body)
    page_text = html.unescape(" ".join(parser.text_chunks))
    lower_text = page_text.lower()
    title = (parser.meta.get("og:title") or parser.meta.get("twitter:title") or " ".join(parser.title_chunks)).strip()
    product_text = (title + " " + source["url"]).lower()
    product_confirmed = any(k.lower() in product_text for k in source.get("product_keywords", ["ps5"]))
    if any(k.lower() in product_text for k in source.get("exclude_keywords", [])):
        return {"state": "excluded", "detail": "Produit reconditionné ou d’occasion"}

    product_nodes: list[dict[str, Any]] = []
    for root in parser.jsonld:
        for node in walk_json(root):
            types = node.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if any(t.lower() in ("product", "individualproduct", "productgroup") for t in types):
                product_nodes.append(node)
    match_terms = [x.lower() for x in source.get("product_match_keywords", [])]
    matching_products = []
    for node in product_nodes:
        node_url = node.get("url") or node.get("@id") or ""
        if isinstance(node_url, dict):
            node_url = node_url.get("@id", "")
        node_text = (str(node.get("name", "")) + " " + str(node_url)).lower()
        if match_terms and any(term in node_text for term in match_terms):
            matching_products.append(node)
    # Never borrow stock from related-product cards. If the page has several
    # products and the target cannot be identified, fall back to visible text.
    if matching_products:
        main_products = matching_products
    elif len(product_nodes) == 1:
        main_products = product_nodes
    else:
        main_products = []
    offers: list[dict[str, Any]] = []
    for node in main_products:
        raw_offers = node.get("offers", [])
        offers.extend(raw_offers if isinstance(raw_offers, list) else [raw_offers])
    availability_values: list[str] = []
    seller_text: list[str] = []
    prices: list[float] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        availability = str(offer.get("availability", "")).strip().lower()
        if availability:
            availability_values.append(availability)
        seller = offer.get("seller") or offer.get("offeredBy") or ""
        seller_text.append(json.dumps(seller, ensure_ascii=False).lower() if isinstance(seller, (dict, list)) else str(seller).lower())
        p = price_number(offer.get("price") or offer.get("priceSpecification"))
        if p is not None:
            prices.append(p)

    for key in ("product:availability", "availability", "og:availability", "itemprop=availability"):
        value = parser.meta.get(key, "").strip().lower()
        if value:
            availability_values.append(value)
    structured_available = any(
        x.rstrip("/").endswith(("instock", "limitedavailability", "onlineonly"))
        or x.strip() in ("in stock", "en stock")
        for x in availability_values
    )
    structured_unavailable = any(
        x.rstrip("/").endswith(("outofstock", "soldout", "discontinued"))
        or x.strip() in ("out of stock", "rupture de stock", "indisponible", "épuisé")
        for x in availability_values
    )
    text_available = bool(re.search(r"\ben stock\b|disponible immédiatement|disponible en ligne|\bin stock\b|available now", lower_text))
    text_unavailable = bool(re.search(r"rupture de stock|indisponible|\bout of stock\b|\bsold out\b|épuisé", lower_text))
    available = structured_available or text_available
    unavailable = structured_unavailable or text_unavailable
    availability_signal = "données structurées" if structured_available else "texte visible explicite (moins fiable)"

    domain = (urllib.parse.urlparse(final_url).hostname or "").lower()
    allowed_domains = [d.lower().lstrip(".") for d in source.get("official_domains", [])]
    domain_ok = any(domain == d or domain.endswith("." + d) for d in allowed_domains)
    seller_aliases = [x.lower() for x in source.get("seller_aliases", [])]
    # The site's domain proves the seller only for first-party official shops.
    first_party = bool(source.get("first_party", False))
    seller_ok = domain_ok and (first_party or any(alias in " ".join(seller_text) for alias in seller_aliases))
    # For pages without machine-readable seller data, accept an explicit visible first-party label.
    if domain_ok and not seller_ok:
        seller_phrases = ("vendu par", "vendu et expédié par", "expédié par", "sold by", "shipped by")
        seller_ok = any(
            re.search(re.escape(phrase) + r"\s*" + re.escape(alias), lower_text)
            for phrase in seller_phrases for alias in seller_aliases
        )
    price = min(prices) if prices else None

    if available and unavailable:
        state, detail = "unknown", "Indices de stock contradictoires ; vérifier la fiche"
        certainty = {"product": product_confirmed, "seller": seller_ok, "price": price is not None, "within_limit": price is not None and (max_price is None or price <= max_price)}
    elif unavailable:
        state, detail = "out_of_stock", "Indisponible"
        certainty = {"product": product_confirmed, "seller": seller_ok, "price": price is not None, "within_limit": price is not None and (max_price is None or price <= max_price)}
    elif available:
        state = "stock_detected"
        certainty = {"product": product_confirmed, "seller": seller_ok, "price": price is not None, "within_limit": price is not None and (max_price is None or price <= max_price)}
        checks = [
            "produit reconnu" if product_confirmed else "produit à vérifier",
            "vendeur officiel confirmé" if seller_ok else "vendeur à vérifier",
            (f"prix {price:.2f} €" + (" dans le plafond" if certainty["within_limit"] else " au-dessus du plafond")) if price is not None else "prix à vérifier",
        ]
        detail = f"Stock détecté ({availability_signal}) ; " + ", ".join(checks)
    else:
        state, detail = "unknown", "Stock non déterminable automatiquement — vérifier la fiche"
        certainty = {"product": product_confirmed, "seller": seller_ok, "price": price is not None, "within_limit": price is not None and (max_price is None or price <= max_price)}
    return {"state": state, "detail": detail, "title": title or source["name"], "url": final_url, "price": price, "certainty": certainty, "availability_signal": availability_signal if available else None}


class Telegram:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 40) -> Any:
        data = urllib.parse.urlencode(payload or {}).encode()
        req = urllib.request.Request(self.base + method, data=data, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Telegram API error"))
        return result["result"]

    def send(self, chat_id: int, message: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"})


def source_status_line(source: dict[str, Any], result: dict[str, Any] | None) -> str:
    if not result:
        return f"• {source['name']} — pas encore vérifié"
    stamp = result.get("checked_at", "")
    return f"• {source['name']} — {result.get('detail', result['state'])} ({stamp})"


def send_sms_if_configured(message: str) -> None:
    """Optional Twilio SMS transport; no-op until all four settings are present."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    to_number = os.environ.get("SMS_TO_NUMBER", "").strip()
    if not all((sid, auth_token, from_number, to_number)):
        return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    credentials = base64.b64encode(f"{sid}:{auth_token}".encode()).decode()
    data = urllib.parse.urlencode({"From": from_number, "To": to_number, "Body": message[:1500]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {credentials}", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Twilio HTTP {response.status}")

def check_sources(config: dict[str, Any], state: dict[str, Any], telegram: Telegram, force: bool = False) -> None:
    results = state.setdefault("results", {})
    subscribers = state.setdefault("subscribers", [])
    timeout = int(config.get("request_timeout_seconds", 20))
    max_price = config.get("max_price_eur")
    if max_price is not None:
        max_price = float(max_price)
    active = [s for s in config.get("sources", []) if s.get("enabled", True)]
    for index, source in enumerate(active):
        previous = results.get(source["id"], {})
        if previous.get("state") == "blocked" and not force:
            continue
        try:
            result = inspect_page(source, timeout, max_price)
        except (OSError, ValueError, RuntimeError) as exc:
            result = {"state": "error", "detail": f"Erreur réseau/lecture : {str(exc)[:120]}"}
        result["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        old_certainty = previous.get("certainty", {})
        certainty = result.get("certainty", {})
        newly_detected = result["state"] == "stock_detected" and previous.get("state") != "stock_detected"
        confidence_improved = result["state"] == "stock_detected" and previous.get("state") == "stock_detected" and certainty != old_certainty
        results[source["id"]] = result
        save_state(state)
        if newly_detected or confidence_improved:
            price_line = f"\nPrix détecté : {result['price']:.2f} €" if result.get("price") is not None else "\nPrix : non confirmé"
            seller_line = "confirmé" if certainty.get("seller") else "à vérifier sur la fiche"
            product_line = "reconnu comme PS5" if certainty.get("product") else "à confirmer comme PS5"
            limit_line = "dans le plafond configuré" if certainty.get("within_limit") else "plafond non confirmé ou dépassé"
            signal = result.get("availability_signal") or "signal du site"
            message = f"🟡 STOCK DÉTECTÉ — vérifie la fiche\n{source['name']}\n{result['title']}\nSignal : {signal}.{price_line}\nVendeur officiel : {seller_line}.\nProduit : {product_line}. Prix : {limit_line}.\n{result['url']}"
            if confidence_improved:
                message = "ℹ️ Mise à jour de confirmation\n" + message
            for chat_id in list(subscribers):
                try:
                    telegram.send(int(chat_id), message)
                except Exception as exc:
                    print(f"Échec d’envoi vers {chat_id}: {exc}")
            try:
                send_sms_if_configured(f"Stock PS5 détecté chez {source['name']}. Vendeur: {seller_line}. Produit: {product_line}. {price_line.strip()}. {result['url']}")
            except Exception as exc:
                print(f"Échec d’envoi SMS : {exc}")
        if result["state"] == "blocked":
            message = f"⚠️ {source['name']} a bloqué la vérification ({result['detail']}). Vérification manuelle requise."
            for chat_id in list(subscribers):
                try:
                    telegram.send(int(chat_id), message)
                except Exception:
                    pass
        if index < len(active) - 1:
            time.sleep(int(config.get("pause_between_sources_seconds", 8)))


def handle_command(text: str, chat_id: int, config: dict[str, Any], state: dict[str, Any], tg: Telegram) -> None:
    command = text.strip().split()[0].split("@", 1)[0].lower() if text.strip() else ""
    subscribers = state.setdefault("subscribers", [])
    if command in ("/start", "/subscribe"):
        if chat_id not in subscribers:
            subscribers.append(chat_id)
            save_state(state)
        tg.send(chat_id, "Abonnement activé. Je t’alerte dès qu’un signal de disponibilité PS5 est détecté, avec le niveau de confirmation du produit, du vendeur et du prix.\nCommandes : /status /sources /check /stop")
    elif command in ("/stop", "/unsubscribe"):
        if chat_id in subscribers:
            subscribers.remove(chat_id)
            save_state(state)
        tg.send(chat_id, "Alertes arrêtées. Envoie /start pour te réabonner.")
    elif command == "/sources":
        names = [f"• {s['name']} — {'active' if s.get('enabled', True) else 'inactive'}" for s in config.get("sources", [])]
        tg.send(chat_id, "Sources configurées :\n" + ("\n".join(names) or "Aucune"))
    elif command == "/status":
        tg.send(chat_id, "Dernières vérifications :\n" + "\n".join(source_status_line(s, state.get("results", {}).get(s["id"])) for s in config.get("sources", []) if s.get("enabled", True)))
    elif command == "/check":
        tg.send(chat_id, "Je vérifie les fiches maintenant. Je t’enverrai un message uniquement si un stock officiel est confirmé.")
        check_sources(config, state, tg, force=True)
        tg.send(chat_id, "Vérification terminée. Consulte /status pour le détail.")
    else:
        tg.send(chat_id, "Commandes : /start, /stop, /status, /sources, /check")


def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN absent. Copie .env.example en .env et renseigne le jeton BotFather.")
    config = read_json(CONFIG_PATH, {})
    if not config.get("sources"):
        raise SystemExit("Aucune source configurée dans sources.json")
    tg = Telegram(token)
    me = tg.call("getMe", timeout=15)
    print(f"Bot @{me.get('username')} démarré. Polling : {config.get('poll_seconds', 300)} s")
    # Ensure long polling works even if this bot previously had a webhook configured.
    try:
        tg.call("deleteWebhook", {"drop_pending_updates": "false"}, timeout=15)
    except Exception as exc:
        print(f"Info webhook Telegram : {exc}")
    state = read_json(STATE_PATH, {"subscribers": [], "results": {}, "offset": 0})
    next_check = 0.0
    while True:
        try:
            updates = tg.call("getUpdates", {"offset": int(state.get("offset", 0)), "timeout": 25, "allowed_updates": json.dumps(["message"])}, timeout=35)
            for update in updates:
                state["offset"] = int(update["update_id"]) + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat = msg.get("chat", {})
                if text.startswith("/") and chat.get("id") is not None:
                    try:
                        handle_command(text, int(chat["id"]), config, state, tg)
                    except Exception as exc:
                        print(f"Commande Telegram : {exc}")
                save_state(state)
            if time.monotonic() >= next_check:
                check_sources(config, state, tg)
                next_check = time.monotonic() + int(config.get("poll_seconds", 300))
        except KeyboardInterrupt:
            print("Arrêt demandé.")
            return
        except urllib.error.HTTPError as exc:
            retry = int(exc.headers.get("Retry-After", "10")) if exc.headers else 10
            print(f"Telegram HTTP {exc.code}; nouvelle tentative dans {retry}s")
            time.sleep(max(5, min(retry, 300)))
        except Exception as exc:
            print(f"Erreur de boucle : {exc}; nouvelle tentative dans 15s")
            time.sleep(15)


if __name__ == "__main__":
    main()



