import asyncio
import os
import random
from typing import Optional, Tuple
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, PreCheckoutQuery
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from duckduckgo_search import DDGS
import redis.asyncio as redis
from langdetect import detect, DetectorFactory
from langdetect.lang_detect_exception import LangDetectException

# ========== КОНФИГ ==========
DetectorFactory.seed = 0

BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")

STARS_PRICE = 1
TOKENS_PER_STAR = 4
TOKEN_COST_PER_CHECK = 1

SUPPORTED_LANGS = {"ru", "en", "es", "pt", "bn", "hi", "ar", "fr", "zh-cn", "ja"}

GRUMPY_INTROS = {
    "ru": [
        "Ох, блин...",
        "Ну ты даёшь...",
        "Слушай, умник...",
        "О господи...",
        "Ну вот опять...",
    ],
    "en": [
        "Oh boy...",
        "You gotta be kidding...",
        "Listen here, smartass...",
        "Oh my god...",
        "Here we go again...",
    ],
}

PARASITE_PHRASES = {
    "ru": [
        "А хотя я хрен его знает.",
        "Впрочем, как всегда, я могу ошибаться.",
        "Но кто меня спрашивает.",
    ],
    "en": [
        "But what do I know.",
        "Then again, I could be wrong.",
        "Not that anyone asked me.",
    ],
}

VERDICT_TEMPLATES = {
    "ru": {
        "ДА": "Да, это правда. Источник: {link}",
        "НЕТ": "Нет, это неправда. Источник: {link}",
        "НЕ ЗНАЮ": "Не могу подтвердить. Источник: {link}",
    },
    "en": {
        "YES": "Yes, that's true. Source: {link}",
        "NO": "No, that's false. Source: {link}",
        "DON'T KNOW": "Can't confirm. Source: {link}",
    },
}

redis_client = None

async def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    return redis_client

async def get_tokens(user_id: int) -> int:
    r = await get_redis()
    val = await r.get(f"user:{user_id}:tokens")
    return int(val) if val else 0

async def set_tokens(user_id: int, tokens: int):
    r = await get_redis()
    await r.set(f"user:{user_id}:tokens", tokens)

async def add_tokens(user_id: int, delta: int):
    current = await get_tokens(user_id)
    await set_tokens(user_id, current + delta)

async def get_ref_code(user_id: int) -> str:
    r = await get_redis()
    code = await r.get(f"user:{user_id}:ref_code")
    if not code:
        code = f"{user_id}_{random.randint(1000, 9999)}"
        await r.set(f"user:{user_id}:ref_code", code)
        await r.set(f"ref_code:{code}", user_id)
    return code

async def get_referrer(user_id: int) -> Optional[int]:
    r = await get_redis()
    ref_by = await r.get(f"user:{user_id}:referred_by")
    return int(ref_by) if ref_by else None

async def set_referrer(user_id: int, referrer_id: int):
    r = await get_redis()
    await r.set(f"user:{user_id}:referred_by", referrer_id)

async def get_ref_stats(user_id: int) -> dict:
    r = await get_redis()
    invited = await r.smembers(f"user:{user_id}:invited")
    tokens_earned = await r.get(f"user:{user_id}:ref_tokens_earned") or 0
    return {
        "invited_count": len(invited),
        "tokens_earned": int(tokens_earned),
    }

async def add_invited(user_id: int, invited_id: int):
    r = await get_redis()
    await r.sadd(f"user:{user_id}:invited", invited_id)

async def add_ref_tokens(user_id: int, amount: int):
    r = await get_redis()
    current = await r.get(f"user:{user_id}:ref_tokens_earned") or 0
    await r.set(f"user:{user_id}:ref_tokens_earned", int(current) + amount)
    await add_tokens(user_id, amount)

async def check_referral_reward(user_id: int):
    referrer_id = await get_referrer(user_id)
    if not referrer_id:
        return
    r = await get_redis()
    spent_key = f"user:{user_id}:total_spent_tokens"
    spent = await r.get(spent_key) or 0
    spent = int(spent)
    reward_given_key = f"user:{referrer_id}:reward_given_for:{user_id}"
    reward_given = await r.get(reward_given_key)
    if spent >= 4 and not reward_given:
        await add_ref_tokens(referrer_id, 8)
        await add_invited(referrer_id, user_id)
        await r.set(reward_given_key, "yes")

async def increment_spent_tokens(user_id: int):
    r = await get_redis()
    key = f"user:{user_id}:total_spent_tokens"
    spent = await r.get(key) or 0
    await r.set(key, int(spent) + 1)
    await check_referral_reward(user_id)

async def detect_language(update: Update) -> str:
    user_lang = update.effective_user.language_code
    if user_lang and user_lang in SUPPORTED_LANGS:
        return user_lang
    text = update.message.text if update.message else ""
    try:
        detected = detect(text)
        if detected in SUPPORTED_LANGS:
            return detected
    except LangDetectException:
        pass
    return "en"

async def search_fact(query: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=2))
            if not results:
                return None, None
            link = results[0]["href"]
            snippet = results[0]["body"][:300]
            return snippet, link
    except Exception:
        return None, None

def generate_grumpy_response(statement: str, search_snippet: Optional[str], link: Optional[str], lang: str) -> str:
    intro = random.choice(GRUMPY_INTROS.get(lang, GRUMPY_INTROS["en"]))
    parasite = random.choice(PARASITE_PHRASES.get(lang, PARASITE_PHRASES["en"]))
    if not search_snippet or not link:
        verdict = "НЕ ЗНАЮ" if lang == "ru" else "DON'T KNOW"
        template = VERDICT_TEMPLATES.get(lang, VERDICT_TEMPLATES["en"])
        key = "НЕ ЗНАЮ" if lang == "ru" else "DON'T KNOW"
        justification = template[key].format(link="#ничего_не_найдено")
    else:
        if any(word in search_snippet.lower() for word in ["true", "yes", "правда", "да"]):
            verdict = "ДА" if lang == "ru" else "YES"
        elif any(word in search_snippet.lower() for word in ["false", "no", "ложь", "нет"]):
            verdict = "НЕТ" if lang == "ru" else "NO"
        else:
            verdict = "НЕ ЗНАЮ" if lang == "ru" else "DON'T KNOW"
        template = VERDICT_TEMPLATES.get(lang, VERDICT_TEMPLATES["en"])
        if verdict == "ДА" or verdict == "YES":
            key = "ДА" if lang == "ru" else "YES"
        elif verdict == "НЕТ" or verdict == "NO":
            key = "НЕТ" if lang == "ru" else "NO"
        else:
            key = "НЕ ЗНАЮ" if lang == "ru" else "DON'T KNOW"
        justification = template[key].format(link=link)
    return f"{intro}\n\n*{verdict}*\n\n{justification}\n\n{parasite}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tokens = await get_tokens(user_id)
    if context.args and len(context.args) > 0:
        ref_code = context.args[0]
        r = await get_redis()
        referrer_id = await r.get(f"ref_code:{ref_code}")
        if referrer_id and int(referrer_id) != user_id:
            if not await get_referrer(user_id):
                await set_referrer(user_id, int(referrer_id))
                await update.message.reply_text("🔗 Реферальный код активирован!")
    text = (
        "🤖 *Ворчун* — фактчекер с характером\n\n"
        f"💰 Баланс: {tokens} токенов\n"
        f"⭐ 1 звезда = {TOKENS_PER_STAR} токенов\n\n"
        "/balance — баланс\n"
        "/buy — купить токены\n"
        "/ref — реферальная ссылка\n\n"
        "Напиши утверждение — проверю и поругаюсь."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tokens = await get_tokens(user_id)
    await update.message.reply_text(f"💰 У тебя {tokens} токенов. 1 проверка = 1 токен.")

async def ref_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    code = await get_ref_code(user_id)
    stats = await get_ref_stats(user_id)
    text = (
        f"🔗 *Твоя ссылка:*\n"
        f"`https://t.me/{context.bot.username}?start={code}`\n\n"
        f"📊 Приглашено: {stats['invited_count']}\n"
        f"💰 Заработано токенов: {stats['tokens_earned']}\n\n"
        f"🎁 Когда друг потратит 4 токена — получишь +8 токенов."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PAYMENT_PROVIDER_TOKEN:
        await update.message.reply_text("❌ Платежи не настроены.")
        return
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="4 токена для Ворчуна",
        description="⭐ 1 звезда = 4 токена",
        payload=f"buy_tokens_{update.effective_user.id}",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice("4 токена", STARS_PRICE)],
    )

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await add_tokens(user_id, TOKENS_PER_STAR)
    await update.message.reply_text(f"✅ Получил {TOKENS_PER_STAR} токенов. А теперь отстань.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await get_tokens(user_id) < TOKEN_COST_PER_CHECK:
        await update.message.reply_text("❌ Нет токенов. Жми /buy")
        return
    lang = await detect_language(update)
    await update.message.chat.send_action(action="typing")
    snippet, link = await search_fact(update.message.text)
    response = generate_grumpy_response(update.message.text, snippet, link, lang)
    await set_tokens(user_id, await get_tokens(user_id) - TOKEN_COST_PER_CHECK)
    await increment_spent_tokens(user_id)
    await update.message.reply_text(response, parse_mode="Markdown", disable_web_page_preview=True)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("ref", ref_command))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤬 Ворчун запущен")
    app.run_polling(allowed_updates=["message", "pre_checkout_query"])

if __name__ == "__main__":
    main()
