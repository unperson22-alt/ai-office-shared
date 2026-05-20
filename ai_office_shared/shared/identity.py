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
# Структура: canonical_lower → {display, repo, railway_service_id, aliases}
# aliases — всё что может прийти снаружи (латиница, UPPERCASE, старые имена)

BOTS: dict[str, dict] = {
    "филли": {
        "display":    "Филли",
        "repo":       "filly-bot",
        "service_id": "5d61d403-feee-455e-9c0d-523f0e7c79d5",
        "aliases":    ["filly", "ФИЛЛИ", "Филли", "FILLY"],
        "role":       "router",
    },
    "билли": {
        "display":    "Билли",
        "repo":       "billy-bot",
        "service_id": "b441ce93-9736-49b3-9b5d-d0c82e715b28",
        "aliases":    ["billy", "БИЛЛИ", "Билли", "BILLY"],
        "role":       "productivity",
    },
    "крисс": {
        "display":    "Крис",
        "repo":       "kriss-bot",
        "service_id": "92f70bbb-70ea-474c-be0d-5cc1c9bd8f4e",
        "aliases":    ["kriss", "крис", "КРИСС", "Крис", "KRISS"],
        "role":       "assistant",
    },
    "эллис": {
        "display":    "Эллис",
        "repo":       "mama-bot",
        "service_id": "fa7c87cf-454c-4946-ab25-6a5091f0ac47",
        "aliases":    ["ellis", "ellice", "ЭЛЛИС", "Эллис", "ELLIS", "mama"],
        "role":       "family",
    },
    "тилли": {
        "display":    "Тилли",
        "repo":       "tilly-bot",
        "service_id": "367e25d7-8410-419d-896d-2cc86cd44efd",
        "aliases":    ["tilly", "ТИЛЛИ", "Тилли", "TILLY"],
        "role":       "lifestyle",
    },
    "милли": {
        "display":    "Милли",
        "repo":       "milly-bot",
        "service_id": "db277aff-6638-4b4a-970e-b016bd753608",
        "aliases":    ["milly", "МИЛЛИ", "Милли", "MILLY"],
        "role":       "business",
    },
    "доктор": {
        "display":    "Доктор",
        "repo":       "doctor-bot",
        "service_id": "d949c4d2-59fa-4cbe-8bb8-a0589a476607",
        "aliases":    ["doctor", "dilly", "ДОКТОР", "Доктор", "DOCTOR", "DILLY"],
        "role":       "health",
    },
    "вилли": {
        "display":    "Вилли",
        "repo":       "villy-bot",
        "service_id": "a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb",
        "aliases":    ["villy", "ВИЛЛИ", "Вилли", "VILLY"],
        "role":       "legal",
    },
    "гослинг": {
        "display":    "Гослинг",
        "repo":       "gosling-bot",
        "service_id": "ed03c9d3-e83f-4675-9f0a-a4d4fc622365",
        "aliases":    ["gosling", "ГОСЛИНГ", "Гослинг", "GOSLING"],
        "role":       "casual",
    },
    "силли": {
        "display":    "Силли",
        "repo":       "ai-office-shared",  # живёт в agents/coder.py
        "service_id": "efa6bd21-91d8-467f-8250-60f8a3853791",
        "aliases":    ["cilly", "силли", "СИЛЛИ", "Силли", "CILLY"],
        "role":       "meta",
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
