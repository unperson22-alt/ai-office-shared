"""
ai_office_shared.shared.identity — единый реестр идентичностей всех ботов офиса.

ПРОБЛЕМА, которую решаем:
    Каждый бот хранил своё имя сам: BOT_NAME = "Билли", BOT_NAME_LOWER = "билли".
    Фили использует UPPERCASE ключи в BOT_URLS, боты пишут lowercase в Redis.
    Один бот — минимум два регистра в разных местах кода.
    Shared library не может надёжно читать ничьи данные без единого источника правды.

РЕШЕНИЕ:
    Один dict BOTS. Все остальные слои импортируют отсюда.
    Никакого хардкода имён в бизнес-логике.

Использование:
    from ai_office_shared.shared.identity import canonical, display, redis_key, BOTS

    canonical("БИЛЛИ")      → "билли"       # для Redis ключей
    canonical("Billy")      → "билли"       # прощает латиницу
    display("билли")        → "Билли"       # для пользователя
    redis_key("билли", "history", 123) → "history:Билли:123"
    redis_key("билли", "logs")         → "office:logs:билли"  (особый формат)

    BOTS.keys()  → все canonical имена
    "билли" in BOTS  → True
"""

from __future__ import annotations
from typing import Optional

# ── Единый реестр ──────────────────────────────────────────────────────────────
# Структура: canonical_lower → {display, repo, service_id, aliases, role,
#                               route_key, url, username, persona}
# aliases   — всё что может прийти снаружи (латиница, UPPERCASE, старые имена)
# route_key — UPPERCASE имя, которым бота адресуют Филли (BOT_URLS) и теги
#             [OFFICE:ИМЯ:...]. У Крисса и Доктора историчеcки два написания:
#             route_key — то, что реально ходит по проводу, второе живёт в aliases.
# url       — база для HTTP /task. Единственный источник: раньше адреса были
#             раскопированы по BOT_URLS, OFFICE_AGENTS и промтам и разъехались.
# persona   — одна строка «кто это и чем занят». Идёт в roster_prompt(), чтобы
#             боты знали друг друга по описанию, а не по догадкам из чата.

BOTS: dict[str, dict] = {
    "филли": {
        "display":    "Филли",
        "repo":       "filly-bot",
        "service_id": "5d61d403-feee-455e-9c0d-523f0e7c79d5",
        "aliases":    ["filly", "ФИЛЛИ", "Филли", "FILLY", "фили", "ФИЛИ"],
        "role":       "router",
        "route_key":  "ФИЛЛИ",
        "url":        "https://filly-bot-production.up.railway.app",
        "username":   None,
        "persona":    "диспетчер офиса: принимает сообщения группы и роутит их нужному боту. Сама в разговор не вступает.",
    },
    "билли": {
        "display":    "Билли",
        "repo":       "billy-bot",
        "service_id": "b441ce93-9736-49b3-9b5d-d0c82e715b28",
        "aliases":    ["billy", "БИЛЛИ", "Билли", "BILLY"],
        "role":       "productivity",
        "route_key":  "БИЛЛИ",
        "url":        "https://billy-bot-production.up.railway.app",
        "username":   "billy_vlad_bot",
        "persona":    "практик без воды: жизнь, мотивация, игры, конкретные следующие шаги. Говорит прямо, с подъёбами. Лучший друг Гослинга.",
    },
    "крисс": {
        "display":    "Крис",
        "repo":       "kriss-bot",
        "service_id": "92f70bbb-70ea-474c-be0d-5cc1c9bd8f4e",
        "aliases":    ["kriss", "крис", "КРИСС", "Крис", "KRISS", "КРИС"],
        "role":       "assistant",
        "route_key":  "КРИС",
        "url":        "https://kriss-bot-production.up.railway.app",
        "username":   "kriss_ai_office_bot",
        "persona":    "личный ассистент Влада: координация, задачи, планирование, напоминания.",
    },
    "эллис": {
        "display":    "Эллис",
        "repo":       "mama-bot",
        "service_id": "fa7c87cf-454c-4946-ab25-6a5091f0ac47",
        "aliases":    ["ellis", "ellice", "ЭЛЛИС", "Эллис", "ELLIS", "mama"],
        "role":       "family",
        "route_key":  "ЭЛЛИС",
        "url":        "https://ellice-bot-production.up.railway.app",
        "username":   "ellice_mom_bot",
        "persona":    "семейный ассистент: мама, близкие Влада. Тёплый тон. В общий трёп не лезет.",
    },
    "тилли": {
        "display":    "Тилли",
        "repo":       "tilly-bot",
        "service_id": "367e25d7-8410-419d-896d-2cc86cd44efd",
        "aliases":    ["tilly", "ТИЛЛИ", "Тилли", "TILLY"],
        "role":       "lifestyle",
        "route_key":  "ТИЛЛИ",
        "url":        "https://tilly-bot-production.up.railway.app",
        "username":   None,
        "persona":    "аналитик крипторынков и глава trading-dept: ТА, макро, ликвидации. Холодная голова.",
    },
    "милли": {
        "display":    "Милли",
        "repo":       "milly-bot",
        "service_id": "db277aff-6638-4b4a-970e-b016bd753608",
        "aliases":    ["milly", "МИЛЛИ", "Милли", "MILLY"],
        "role":       "business",
        "route_key":  "МИЛЛИ",
        "url":        "https://milly-bot-production.up.railway.app",
        "username":   None,
        "persona":    "бизнес-ассистент: автоматизация, продажи, деньги. Мыслит результатами, говорит цифрами.",
    },
    "доктор": {
        "display":    "Доктор",
        "repo":       "doctor-bot",
        "service_id": "d949c4d2-59fa-4cbe-8bb8-a0589a476607",
        "aliases":    ["doctor", "dilly", "ДОКТОР", "Доктор", "DOCTOR", "DILLY", "Дилли"],
        "role":       "health",
        "route_key":  "ДИЛЛИ",
        "url":        "https://dilly-bot-production-4a9b.up.railway.app",
        "username":   None,
        "persona":    "советник по здоровью: питание, тренировки, сон, медицина. Опирается на науку.",
    },
    "вилли": {
        "display":    "Вилли",
        "repo":       "villy-bot",
        "service_id": "a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb",
        "aliases":    ["villy", "ВИЛЛИ", "Вилли", "VILLY"],
        "role":       "design",
        "route_key":  "ВИЛЛИ",
        "url":        "https://villy-bot-production.up.railway.app",
        "username":   None,
        "persona":    "дизайнер и арт-директор: UI/UX, визуал, эстетика. Есть насмотренность и мнение.",
    },
    "гослинг": {
        "display":    "Гослинг",
        "repo":       "gosling-bot",
        "service_id": "ed03c9d3-e83f-4675-9f0a-a4d4fc622365",
        "aliases":    ["gosling", "ГОСЛИНГ", "Гослинг", "GOSLING"],
        "role":       "casual",
        "route_key":  "ГОСЛИНГ",
        "url":        "https://gosling-bot-production.up.railway.app",
        "username":   "gosling_friend_bot",
        "persona":    "персонаж чата, не ассистент: абсурд, авантюрные схемы, оживляет переписку. Лучший друг Билли.",
    },
    "силли": {
        "display":    "Силли",
        "repo":       "ai-office-shared",  # живёт в agents/coder.py
        "service_id": "efa6bd21-91d8-467f-8250-60f8a3853791",
        "aliases":    ["cilly", "силли", "СИЛЛИ", "Силли", "CILLY"],
        "role":       "meta",
        "route_key":  "СИЛЛИ",
        "url":        "https://ai-office-shared-production.up.railway.app",
        "username":   None,
        "persona":    "тимлид и кодер офиса: код, баги, деплой Railway, аудит ботов. Чинит остальных.",
    },
    "пророк": {
        "display":    "Пророк",
        "repo":       "prophet-bot",
        "service_id": None,
        "aliases":    ["prophet", "ПРОРОК", "Пророк", "PROPHET"],
        "role":       "forecast",
        "route_key":  "ПРОРОК",
        "url":        "https://prophet-bot-production-df65.up.railway.app",
        "username":   None,
        "persona":    "советчик по решениям: сценарии «что будет если», взвешенные прогнозы.",
    },
    "пилли": {
        "display":    "Пилли",
        "repo":       "pilly-bot",
        "service_id": None,
        "aliases":    ["pilly", "ПИЛЛИ", "Пилли", "PILLY"],
        "role":       "image",
        "route_key":  "ПИЛЛИ",
        "url":        "https://pilly-bot-production.up.railway.app",
        "username":   None,
        "persona":    "генерация изображений: арт, иллюстрации. Отвечает картинкой, а не текстом.",
    },
    "марти": {
        "display":    "Марти",
        "repo":       "marketing-dept",
        "service_id": "8fb51207",
        "aliases":    ["marty", "МАРТИ", "Марти", "MARTY"],
        "role":       "marketing",
        "route_key":  "МАРТИ",
        "url":        "https://marty-bot-production.up.railway.app",
        "username":   "marty_office_bot",
        "persona":    "CMO и глава marketing-dept: контент, SMM, UGC, блогеры, бренды, реклама.",
    },
    "нэлли": {
        "display":    "Нэлли",
        "repo":       "marketing-dept",
        "service_id": None,
        "aliases":    ["nelli", "НЭЛЛИ", "Нэлли", "NELLI", "нелли", "Нелли"],
        "role":       "marketing",
        "route_key":  "НЭЛЛИ",
        "url":        "https://nelli-bot-production.up.railway.app",
        "username":   "nelli_assistant_bot",
        "persona":    "ноготочки и nail-бизнес, семейный маркетинг.",
    },
    "рэй": {
        "display":    "Рэй",
        "repo":       "ray-bot",
        "service_id": None,
        "aliases":    ["ray", "РЭЙ", "Рэй", "RAY", "рей", "Рей"],
        "role":       "marketing",
        "route_key":  "РЭЙ",
        "url":        "https://ray-bot-production-d754.up.railway.app",
        "username":   "ray_contentment_bot",
        "persona":    "партнёрки, travel, affiliate, контент. Бот брата Влада — Игната.",
    },
}

# ── Обратный индекс: любой alias → canonical ───────────────────────────────────
_ALIAS_INDEX: dict[str, str] = {}
for _canon, _meta in BOTS.items():
    _ALIAS_INDEX[_canon] = _canon                    # сам себе alias
    _ALIAS_INDEX[_canon.upper()] = _canon            # UPPERCASE версия
    for _alias in _meta.get("aliases", []):
        _ALIAS_INDEX[_alias] = _canon
        _ALIAS_INDEX[_alias.lower()] = _canon        # на случай смешанного регистра


def canonical(name: str) -> Optional[str]:
    """
    Нормализует любое имя бота до canonical lowercase.

    canonical("БИЛЛИ") → "билли"
    canonical("Billy") → "билли"
    canonical("мама")  → None  (неизвестный бот)
    """
    if not name:
        return None
    result = _ALIAS_INDEX.get(name) or _ALIAS_INDEX.get(name.lower())
    return result


def display(name: str) -> Optional[str]:
    """
    Красивое имя для вывода пользователю.

    display("билли") → "Билли"
    display("BILLY") → "Билли"
    """
    canon = canonical(name)
    if canon is None:
        return None
    return BOTS[canon]["display"]


def service_id(name: str) -> Optional[str]:
    """Railway service ID по любому варианту имени."""
    canon = canonical(name)
    if canon is None:
        return None
    return BOTS[canon]["service_id"]


def redis_key(name: str, prefix: str, *parts) -> Optional[str]:
    """
    Строит Redis ключ в правильном формате для данного бота.

    Форматы по prefix:
        "logs"    → office:logs:{canon}:{parts[0]}   (дата)
        "quality" → office:quality:{canon}
        "history" → history:{Display}:{parts[0]}     (user_id) — старый формат, совместимость
        "notes"   → notes:{Display}:{parts[0]}       (user_id) — старый формат, совместимость
        иной      → {prefix}:{canon}[:{parts}]

    Примеры:
        redis_key("билли", "logs", "2026-05-20")   → "office:logs:билли:2026-05-20"
        redis_key("билли", "quality")              → "office:quality:билли"
        redis_key("билли", "history", 123)         → "history:Билли:123"
    """
    canon = canonical(name)
    if canon is None:
        return None
    disp = BOTS[canon]["display"]

    if prefix == "logs":
        date = parts[0] if parts else ""
        return f"office:logs:{canon}:{date}" if date else f"office:logs:{canon}"
    if prefix == "quality":
        return f"office:quality:{canon}"
    if prefix in ("history", "notes"):
        # Старый формат: Display-имя для обратной совместимости
        suffix = f":{parts[0]}" if parts else ""
        return f"{prefix}:{disp}{suffix}"

    # Generic
    suffix = ":" + ":".join(str(p) for p in parts) if parts else ""
    return f"{prefix}:{canon}{suffix}"


def all_bots() -> list[str]:
    """Список всех canonical имён."""
    return list(BOTS.keys())


def bots_by_role(role: str) -> list[str]:
    """Canonical имена ботов с заданной ролью."""
    return [k for k, v in BOTS.items() if v.get("role") == role]


def route_key(name: str) -> Optional[str]:
    """
    UPPERCASE имя, которым бота адресуют по проводу (BOT_URLS, [OFFICE:ИМЯ:...]).

    route_key("доктор") → "ДИЛЛИ"   # исторически так, а не "ДОКТОР"
    route_key("КРИСС")  → "КРИС"
    """
    canon = canonical(name)
    return BOTS[canon]["route_key"] if canon else None


def url(name: str) -> Optional[str]:
    """База HTTP для /task по любому варианту имени."""
    canon = canonical(name)
    return BOTS[canon].get("url") if canon else None


def username(name: str) -> Optional[str]:
    """Telegram @username бота (без @), если известен."""
    canon = canonical(name)
    return BOTS[canon].get("username") if canon else None


def bot_by_username(uname: str) -> Optional[str]:
    """
    Canonical имя по Telegram @username. Нужно чтобы понять, КОМУ отвечают
    реплаем в группе: у апдейта есть только username автора.

    bot_by_username("@gosling_friend_bot") → "гослинг"
    """
    if not uname:
        return None
    u = uname.lstrip("@").lower()
    for canon, meta in BOTS.items():
        known = meta.get("username")
        if known and known.lower() == u:
            return canon
    return None


# ── Люди офиса ─────────────────────────────────────────────────────────────────
# ПРОБЛЕМА, которую решаем (инцидент 2026-08-07):
#     Telegram-имя Влада — «Yodka». Групповая история пишется по first_name, то есть
#     строками «Yodka: ...», а в системных промтах ботов сказано «Влад — шеф».
#     Для модели это два разных человека: Гослинг обращался к Билли «Йодка», а на
#     вопрос «ты нас создал?» отвечал так, будто Влад и Йодка — разные люди.
# РЕШЕНИЕ:
#     Один реестр людей с алиасами. roster_prompt() отдаёт его ботам явным текстом,
#     а не оставляет модели догадываться по обрывкам чата.

PEOPLE: dict[str, dict] = {
    "влад": {
        "display": "Влад",
        "user_id": 391077101,
        "aliases": ["Yodka", "yodka", "Йодка", "йодка", "ЙОДКА", "Влад", "ВЛАД",
                    "vlad", "Vlad"],
        "role":    "владелец офиса, создал всех ботов. Курьер DPD в Германии, "
                   "строит бизнес на автоматизации.",
    },
    "лук": {
        "display": "Лук",
        "user_id": 331989769,
        "aliases": ["Лук", "ЛУК", "luk", "Luk", "Саша", "саша", "sasha"],
        "role":    "друг Влада со школы, свой человек в офисе. Тестирует ботов.",
    },
    "ангелина": {
        "display": "Ангелина",
        "user_id": 7354462052,
        "aliases": ["Ангелина", "ангелина", "angelina", "Angelina", "Геля"],
        "role":    "девушка Влада.",
    },
    "яна": {
        "display": "Яна",
        "user_id": 8993567246,   # новый аккаунт, подтверждён Владом 2026-08-12
        "aliases": ["Яна", "яна", "yana", "Yana"],
        "role":    "коллега, есть доступ к Криссу.",
    },
    "мама": {
        "display": "Мама",
        "user_id": 6507691976,
        "aliases": ["мама", "Мама", "mom", "мать"],
        "role":    "мама Влада, общается через Эллис в отдельном канале.",
    },
    "игнат": {
        "display": "Игнат",
        "user_id": 1497200536,
        "aliases": ["Игнат", "игнат", "ignat", "Ignat"],
        "role":    "брат Влада, для него сделан бот Рэй.",
    },
}

# ⚠️ TODO(уточнить у Влада): ID 675773302 числится Луком и в Notion, и в
# kriss-bot ALLOWED_USERS. Влад 2026-08-12 подтвердил, что Лук — 331989769, а
# Ангелина — 7354462052. Чей 675773302 — не выяснено. Доступ намеренно НЕ
# отзываем: молча выкинуть живого человека из вайтлиста хуже, чем оставить
# помеченным. Разобраться и либо назвать, либо убрать из ALLOWED_USERS.
#
# ⚠️ У Яны с 2026-08-12 НОВЫЙ аккаунт — 8993567246. Старый (по Notion 879704934)
# здесь не значится и в коде нигде не захардкожен. ВАЖНО: сам доступ к ботам
# даёт не этот реестр, а Railway-переменная ALLOWED_USERS у kriss-bot / mama-bot /
# billy-bot / family-dept-elliss. Правка PEOPLE влияет только на то, как боты
# НАЗЫВАЮТ человека (roster_prompt); чтобы Яна снова могла писать Криссу, новый
# ID должен попасть в env — это отдельное действие в Railway.

_PERSON_INDEX: dict[str, str] = {}
for _canon_p, _meta_p in PEOPLE.items():
    _PERSON_INDEX[_canon_p] = _canon_p
    for _alias_p in _meta_p.get("aliases", []):
        _PERSON_INDEX[_alias_p.lower()] = _canon_p

_PERSON_BY_ID: dict[int, str] = {
    m["user_id"]: k for k, m in PEOPLE.items() if m.get("user_id")
}


def person(user_id: int) -> Optional[str]:
    """Canonical имя человека по Telegram user_id. None — неизвестный."""
    return _PERSON_BY_ID.get(int(user_id)) if user_id else None


def person_by_alias(name: str) -> Optional[str]:
    """
    Canonical имя человека по любому написанию.

    person_by_alias("Yodka") → "влад"
    person_by_alias("Влад")  → "влад"
    """
    if not name:
        return None
    return _PERSON_INDEX.get(name.strip().lower())


def person_display(name_or_id) -> Optional[str]:
    """Красивое имя человека по user_id или по любому алиасу."""
    canon = (person(name_or_id) if isinstance(name_or_id, int)
             else person_by_alias(str(name_or_id)))
    return PEOPLE[canon]["display"] if canon else None


def who_is(name: str) -> Optional[str]:
    """
    Единая точка «кто это»: сначала люди, потом боты. Возвращает display-имя
    или None. Нужна там, где на входе просто строка из чата.
    """
    return person_display(name) or display(name)


# ── Ростер для системного промта ───────────────────────────────────────────────

def roster_prompt(me: str = "", include_people: bool = True) -> str:
    """
    Детерминированный блок «кто есть кто» для системного промта бота.

    Раньше эту роль играл `office:members` — профиль команды, который Haiku
    сочиняла по сырой групповой истории. Из строк «Yodka: ...» она выводила
    участника «Йодка», отдельного от Влада, и боты это подхватывали. Здесь
    ничего не выводится: список фиксированный, алиасы перечислены явно.

    Args:
        me: canonical/display имя бота, для которого строим блок — он будет
            помечен как «это ты» и исключён из списка коллег.
    """
    me_canon = canonical(me) if me else None

    lines = ["== КТО ЕСТЬ КТО В ОФИСЕ =="]

    if include_people:
        lines.append("")
        lines.append("Люди:")
        for _, meta in PEOPLE.items():
            aliases = [a for a in meta.get("aliases", [])
                       if a.lower() != meta["display"].lower()]
            alias_part = f" (также: {', '.join(sorted(set(aliases))[:4])})" if aliases else ""
            lines.append(f"— {meta['display']}{alias_part} — {meta['role']}")
        lines.append(
            "ВАЖНО: Влад, Yodka и Йодка — ОДИН человек, владелец офиса. "
            "Это его единственный аккаунт. Не обращайся к нему как к двум разным людям."
        )

    lines.append("")
    lines.append("Боты офиса (это НЕ люди, это твои коллеги):")
    for canon, meta in BOTS.items():
        mark = " ← это ТЫ" if canon == me_canon else ""
        lines.append(f"— {meta['display']} — {meta['persona']}{mark}")

    lines.append("")
    lines.append(
        "Если сообщение помечено «[от X]» — его написал X, а не Влад. "
        "Обращайся к автору по имени. Если не уверен, кто пишет — спроси, "
        "не выдумывай имя."
    )
    return "\n".join(lines)
