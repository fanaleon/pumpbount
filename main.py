"""
pump.fun/go Bounties Bot para Telegram
--------------------------------------
Monitorea pump.fun/go/bounties y publica automáticamente
las nuevas bounties OPEN en un canal de Telegram.

Requisitos:
    pip install requests python-telegram-bot playwright
    playwright install chromium

Variables de entorno (en Railway o .env):
    TELEGRAM_TOKEN   -> Token del bot (de @BotFather)
    TELEGRAM_CHAT_ID -> ID del canal (ej: @micanal o -100xxxxxxxxxx)
    CHECK_INTERVAL   -> Segundos entre cada chequeo (default: 120)
    MIN_VALUE_USD    -> Valor mínimo en USD para notificar (default: 0)
"""

import os
import json
import time
import logging
import asyncio
from datetime import datetime
from pathlib import Path

import requests
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from playwright.sync_api import sync_playwright

# ─── CONFIG ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@tu_canal_aqui")
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "120"))   # segundos
MIN_VALUE_USD    = float(os.getenv("MIN_VALUE_USD", "0"))     # filtro de valor mínimo

# Archivo local donde se guardan los IDs ya vistos (para no repetir)
SEEN_FILE = Path("seen_bounties.json")

# ─── LOGGING ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── PERSISTENCIA ───────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ─── SCRAPING ───────────────────────────────────────────────────────────────────

def scrape_bounties() -> list[dict]:
    """
    Usa Playwright para renderizar pump.fun/go/bounties (React/SPA)
    y extraer las bounties OPEN del DOM.
    """
    bounties = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        log.info("Cargando pump.fun/go/bounties ...")
        page.goto("https://pump.fun/go/bounties", wait_until="networkidle", timeout=30000)

        # Esperar que carguen las cards
        try:
            page.wait_for_selector("a[href*='/coin/']", timeout=15000)
        except Exception:
            log.warning("No se encontraron bounties en el DOM.")
            browser.close()
            return []

        # Extraer datos de cada bounty OPEN
        cards = page.query_selector_all("div.bounty-card, div[class*='bounty'], article")

        # Fallback: buscar por patrón de texto OPEN + valor
        # pump.fun renderiza todo en divs sin clases semánticas fijas,
        # así que capturamos el HTML y parseamos manualmente.
        html = page.content()
        browser.close()

    # ── Parser alternativo vía requests + regex para la API interna ──
    # pump.fun llama a su propia API REST mientras carga la página.
    # Interceptamos esa llamada directamente (más confiable que parsear HTML).
    bounties = fetch_via_api()
    return bounties


def fetch_via_api() -> list[dict]:
    """
    pump.fun expone un endpoint REST interno que devuelve las bounties.
    Lo llamamos directamente — es lo mismo que hace el frontend.
    """
    url = "https://client-api-2-74b1891ee9f9.herokuapp.com/bounties"
    params = {
        "status": "open",
        "limit": 50,
        "offset": 0,
        "sort": "created_timestamp",
        "order": "DESC",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://pump.fun/",
        "Origin": "https://pump.fun",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        bounties = []
        items = data if isinstance(data, list) else data.get("bounties", data.get("data", []))

        for item in items:
            # Adaptamos a los campos que pump.fun suele devolver
            bounty_id   = str(item.get("id") or item.get("bounty_id") or "")
            title       = item.get("title") or item.get("name") or "Sin título"
            description = item.get("description") or ""
            usd_value   = float(item.get("usd_value") or item.get("total_value_usd") or 0)
            coin_addr   = item.get("mint") or item.get("coin_address") or ""
            coin_ticker = item.get("ticker") or item.get("symbol") or "???"
            time_left   = item.get("time_left") or item.get("expires_in") or ""
            submissions = int(item.get("submission_count") or item.get("submissions") or 0)
            status      = (item.get("status") or "").upper()

            if status and status != "OPEN":
                continue

            bounty_url = f"https://pump.fun/coin/{coin_addr}" if coin_addr else "https://pump.fun/go/bounties"

            bounties.append({
                "id":          bounty_id,
                "title":       title,
                "description": description[:200] + ("..." if len(description) > 200 else ""),
                "usd_value":   usd_value,
                "coin_ticker": coin_ticker,
                "coin_addr":   coin_addr,
                "time_left":   time_left,
                "submissions": submissions,
                "url":         bounty_url,
            })

        log.info(f"API devolvió {len(bounties)} bounties OPEN.")
        return bounties

    except Exception as e:
        log.error(f"Error llamando API de pump.fun: {e}")
        # Fallback: scraping directo de la página HTML estática
        return scrape_html_fallback()


def scrape_html_fallback() -> list[dict]:
    """
    Fallback: parsea la página /go/bounties con BeautifulSoup.
    Solo funciona si pump.fun renderiza algo en el HTML inicial (SSR parcial).
    """
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(
            "https://pump.fun/go/bounties",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        soup = BeautifulSoup(resp.text, "html.parser")

        # Buscar links a bounties individuales
        bounties = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/bounty/" in href or "/go/" in href:
                bounties.append({
                    "id":          href,
                    "title":       link.get_text(strip=True)[:100],
                    "description": "",
                    "usd_value":   0,
                    "coin_ticker": "???",
                    "coin_addr":   "",
                    "time_left":   "",
                    "submissions": 0,
                    "url":         f"https://pump.fun{href}" if href.startswith("/") else href,
                })
        return bounties
    except Exception as e:
        log.error(f"Fallback HTML también falló: {e}")
        return []

# ─── TELEGRAM ───────────────────────────────────────────────────────────────────

def format_message(b: dict) -> str:
    value_str = f"${b['usd_value']:,.2f}" if b['usd_value'] > 0 else "Ver en sitio"
    ticker    = f"${b['coin_ticker']}" if b['coin_ticker'] != "???" else ""

    lines = [
        f"🎯 *Nueva Bounty OPEN*",
        f"",
        f"📋 *{b['title']}*",
    ]
    if b["description"]:
        lines.append(f"_{b['description']}_")
    lines += [
        f"",
        f"💰 Premio: *{value_str}*  {ticker}",
    ]
    if b["time_left"]:
        lines.append(f"⏳ Tiempo: {b['time_left']}")
    if b["submissions"] > 0:
        lines.append(f"👥 Submissions: {b['submissions']}")
    lines += [
        f"",
        f"🔗 pump\\.fun/go",
    ]
    return "\n".join(lines)


async def send_bounty(bot: Bot, bounty: dict):
    text = format_message(bounty)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📌 Ver y Aplicar →", url=bounty["url"])
    ]])
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
        disable_web_page_preview=False,
    )
    log.info(f"  ✅ Enviada: {bounty['title'][:60]}")


async def send_startup_message(bot: Bot):
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🤖 *Bot de Bounties activo*\n"
            f"Monitoreando pump\\.fun/go cada {CHECK_INTERVAL}s\n"
            f"Filtro mínimo: ${MIN_VALUE_USD:,.0f} USD"
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

# ─── LOOP PRINCIPAL ─────────────────────────────────────────────────────────────

async def main():
    bot  = Bot(token=TELEGRAM_TOKEN)
    seen = load_seen()

    log.info("🚀 Bot iniciado.")
    await send_startup_message(bot)

    while True:
        log.info(f"--- Chequeando bounties ({datetime.now().strftime('%H:%M:%S')}) ---")

        try:
            bounties = scrape_bounties()
            nuevas   = 0

            for b in bounties:
                bid = b["id"]
                if not bid or bid in seen:
                    continue
                if b["usd_value"] < MIN_VALUE_USD:
                    continue

                await send_bounty(bot, b)
                seen.add(bid)
                nuevas += 1
                await asyncio.sleep(2)  # pausa entre mensajes

            save_seen(seen)
            log.info(f"Nuevas enviadas: {nuevas} / Total vistas: {len(seen)}")

        except Exception as e:
            log.error(f"Error en el loop: {e}")

        log.info(f"Esperando {CHECK_INTERVAL}s ...")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
