"""ai_office_shared.shared.failover — единый вход call_llm() с лестницей замен.

Реализация стандарта FAILOVER_ARCHITECTURE.md:
    primary → backup(ы) того же класса → downgrade на класс ниже → Ollama → алерт.

- transient-ошибки (Timeout / 429 / 5xx / 529 / connection) → retry с backoff 2/4/8 с;
- fatal / исчерпан retry → следующая модель в цепочке;
- stop_reason == "refusal" → следующая модель БЕЗ ретрая тем же промтом;
- сработал бэкап → WARNING + degraded=True ("младший брат поднял старшего");
- упала вся цепочка, включая Ollama → CRITICAL + сквозной алерт владельцу + degraded-stub.

Совместимость: возвращает объект `LLMResult` с `.content[0].text` — drop-in замена
для ботовых `_anthropic_call(...)` и прямых `client.messages.create(...)`.

РАНТАЙМ-ИСТОЧНИК ИСТИНЫ — `DEFAULT_CHAINS` ниже (anthropic+ollama, под реальный парк
ключей офиса). `failover_chains.yaml` в корне репо — человекочитаемый оверрайд/документация
(включая точки расширения OpenAI/Google/DeepSeek); читается лениво только если установлен
pyyaml и задан путь (env FAILOVER_CHAINS_PATH или файл рядом).

Не бросает исключений наружу: в худшем случае возвращает degraded-stub.
"""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence, Union

from ai_office_shared.shared.ollama import try_ollama

logger = logging.getLogger("ai_office_shared.failover")

# ── Параметры по умолчанию ────────────────────────────────────────────────────
RETRIES = 3
BACKOFF = (2, 4, 8)  # секунды между попытками одной модели на transient-ошибке
DEGRADED_STUB = (
    "⚠️ Сервис временно деградировал — модели недоступны. "
    "Уведомление отправлено, попробуйте чуть позже."
)

# ── Лестница моделей (рантайм: только провайдеры с ключами у офиса) ───────────
# Класс модели для выбора версии промта (full / lite) — см. §5.4.2 стандарта.
_FULL_PREFIXES = ("claude-opus", "claude-fable", "claude-sonnet")
_LITE_PREFIXES = ("claude-haiku", "gemma", "gpt-4o-mini", "o1-mini", "gemini-1.5-flash")


def model_tier(model: str) -> str:
    """'full' для сильных моделей (opus/fable/sonnet), 'lite' для лёгких (haiku/gemma/mini/flash)."""
    m = (model or "").lower()
    if any(m.startswith(p) for p in _LITE_PREFIXES):
        return "lite"
    if any(m.startswith(p) for p in _FULL_PREFIXES):
        return "full"
    return "full"  # незнакомую модель считаем сильной — не урезаем промт зря


# role → [primary, *backups]; локальный рубеж (Ollama) добавляется всегда последним.
DEFAULT_CHAINS: dict[str, list[str]] = {
    "strategist": ["claude-opus-4-8", "claude-sonnet-4-6"],
    "coder":      ["claude-opus-4-8", "claude-sonnet-4-6"],
    "creative":   ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "data":       ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "operator":   ["claude-haiku-4-5-20251001"],
}

# Авто-бэкапы для bare-вызовов (когда передан только model=, без role/models).
_DOWNGRADE = {
    "opus": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "fable": ["claude-opus-4-8", "claude-sonnet-4-6"],
    "sonnet": ["claude-haiku-4-5-20251001"],
    "haiku": [],
}


def _auto_backups(model: str) -> list[str]:
    m = (model or "").lower()
    for key, backups in _DOWNGRADE.items():
        if key in m:
            return [b for b in backups if b != model]
    return []


# ── YAML-оверрайд (опциональный, без жёсткой зависимости от pyyaml) ──────────
def _load_yaml_chains() -> Optional[dict[str, list[str]]]:
    path = os.environ.get("FAILOVER_CHAINS_PATH")
    if not path:
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.debug("pyyaml не установлен — использую DEFAULT_CHAINS")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        out: dict[str, list[str]] = {}
        for role, spec in raw.items():
            if not isinstance(spec, dict):
                continue
            primary = spec.get("primary")
            pmodel = primary.get("model") if isinstance(primary, dict) else primary
            chain = [pmodel] if pmodel else []
            for b in spec.get("backups", []) or []:
                bm = b.get("model") if isinstance(b, dict) else b
                # берём только anthropic-провайдера в рантайм-цепочку
                prov = b.get("provider") if isinstance(b, dict) else "anthropic"
                if bm and prov in (None, "anthropic"):
                    chain.append(bm)
            if chain:
                out[role] = chain
        return out or None
    except Exception as e:  # noqa: BLE001 — конфиг не должен ронять рантайм
        logger.info("Не удалось прочитать FAILOVER_CHAINS_PATH=%s: %s", path, e)
        return None


_YAML_CHAINS = _load_yaml_chains()


def _chain_for_role(role: str) -> list[str]:
    if _YAML_CHAINS and role in _YAML_CHAINS:
        return list(_YAML_CHAINS[role])
    return list(DEFAULT_CHAINS.get(role, DEFAULT_CHAINS["operator"]))


# ── Результат ─────────────────────────────────────────────────────────────────
class LLMResult:
    """Унифицированный ответ. `.content[0].text` совместим с anthropic и OllamaResult."""

    __slots__ = ("content", "model", "served_by", "degraded", "error", "raw")

    def __init__(self, raw: Any, served_by: str, degraded: bool = False,
                 error: Optional[str] = None):
        self.raw = raw
        self.content = getattr(raw, "content", [])
        self.model = getattr(raw, "model", served_by)
        self.served_by = served_by
        self.degraded = degraded
        self.error = error

    @property
    def text(self) -> str:
        try:
            for block in self.content:
                t = getattr(block, "text", None)
                if t:
                    return t
        except Exception:  # noqa: BLE001
            pass
        return ""


# ── Классификация ошибок / ответа ────────────────────────────────────────────
_TRANSIENT_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_TRANSIENT_SUBSTR = (
    "429", "500", "502", "503", "504", "529",
    "overloaded", "rate limit", "rate_limit", "timed out", "timeout",
    "temporarily", "connection", "econnreset",
)


def _is_transient(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_CODES:
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return True
    s = str(exc).lower()
    return any(k in s for k in _TRANSIENT_SUBSTR)


def _is_refusal(resp: Any) -> bool:
    return getattr(resp, "stop_reason", None) == "refusal"


def _refusal_cat(resp: Any) -> str:
    det = getattr(resp, "stop_details", None)
    return getattr(det, "category", None) or "unknown"


# ── Промт под уровень модели (graceful degradation §5.4.2) ────────────────────
SystemSpec = Union[None, str, dict, Callable[[str], Optional[str]]]


def _system_for(system: SystemSpec, model: str) -> Optional[str]:
    if system is None or isinstance(system, str):
        return system
    if callable(system):
        return system(model_tier(model))
    if isinstance(system, dict):  # {"full": "...", "lite": "..."}
        tier = model_tier(model)
        return system.get(tier) or system.get("full") or system.get("lite")
    return None


# ── Дедуп критических алертов (in-process, role+hour) ─────────────────────────
_ALERT_SEEN: dict[str, float] = {}
_ALERT_TTL = 3600.0


def _alert_once(key: str) -> bool:
    now = time.time()
    last = _ALERT_SEEN.get(key, 0.0)
    if now - last < _ALERT_TTL:
        return False
    _ALERT_SEEN[key] = now
    return True


def _degraded_stub(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)], stop_reason="degraded", model="none"
    )


# ── Основная точка входа ──────────────────────────────────────────────────────
def call_llm(
    client: Any,
    *,
    messages: list,
    role: Optional[str] = None,
    model: Optional[str] = None,
    models: Optional[Sequence[str]] = None,
    system: SystemSpec = None,
    try_local_first: bool = False,
    owner_alert: Optional[Callable[[str], Any]] = None,
    retries: int = RETRIES,
    backoff: Sequence[int] = BACKOFF,
    stub_text: str = DEGRADED_STUB,
    log_ctx: str = "",
    **create_kwargs: Any,
) -> LLMResult:
    """Вызвать LLM по лестнице замен с retry/деградацией/эскалацией.

    Цепочка моделей определяется (в порядке приоритета):
        models=[...]  →  role="operator"  →  model="..." (+ авто-бэкапы).
    Локальный рубеж Ollama добавляется последним всегда.

    Args:
        client: anthropic.Anthropic (sync). Может быть None — тогда только Ollama.
        messages: список сообщений (формат Anthropic).
        role: роль из DEFAULT_CHAINS (strategist/coder/creative/data/operator).
        model: одна модель (если нет role/models) — к ней добавятся авто-бэкапы.
        models: явная цепочка моделей (переопределяет role/model).
        system: str | {"full":..,"lite":..} | callable(tier)->str — промт под уровень.
        try_local_first: True = сначала Ollama (режим экономии для разговорных ботов),
            успех НЕ считается деградацией; иначе Ollama — последний рубеж (degraded=True).
        owner_alert: callable(text) для сквозного алерта владельцу при каскаде (best-effort).
        retries/backoff: ретраи и паузы на transient-ошибке одной модели.
        **create_kwargs: прочее для client.messages.create (max_tokens, tools, …).

    Returns:
        LLMResult (.content[0].text совместим с anthropic/OllamaResult; .degraded, .served_by).
    """
    # 1. Собрать цепочку моделей
    if models:
        chain = list(models)
    elif role:
        chain = _chain_for_role(role)
    elif model:
        chain = [model] + _auto_backups(model)
    else:
        chain = list(DEFAULT_CHAINS["operator"])

    label = log_ctx or role or (chain[0] if chain else "?")
    primary = chain[0] if chain else "?"

    # 2. Локально-первый режим (экономия) — успех считается штатным
    if try_local_first:
        ol = try_ollama(messages, _system_for(system, "gemma3:4b"))
        if ol is not None:
            return LLMResult(ol, "ollama", degraded=False)

    # 3. Основная цепочка: primary → backups
    last_err: Optional[str] = None
    if client is not None:
        for idx, mdl in enumerate(chain):
            for attempt in range(max(1, retries)):
                try:
                    kw = dict(create_kwargs)
                    kw["model"] = mdl
                    kw["messages"] = messages
                    sysmsg = _system_for(system, mdl)
                    if sysmsg is not None:
                        kw["system"] = sysmsg
                    resp = client.messages.create(**kw)

                    if _is_refusal(resp):  # policy — без ретрая тем же промтом
                        last_err = f"refusal:{_refusal_cat(resp)}"
                        logger.info("[%s] %s refusal → следующая модель", label, mdl)
                        break

                    if idx > 0:  # сработал бэкап → деградация ("брат поднял старшего")
                        logger.warning(
                            "[%s] %s недоступна (%s) → ответ выдан %s. Деградация уровня.",
                            label, primary, last_err, mdl,
                        )
                    return LLMResult(resp, mdl, degraded=(idx > 0))

                except Exception as e:  # noqa: BLE001
                    last_err = f"{type(e).__name__}: {e}"
                    if _is_transient(e) and attempt < retries - 1:
                        time.sleep(backoff[min(attempt, len(backoff) - 1)])
                        continue
                    logger.info("[%s] %s упала (%s) → следующая модель", label, mdl, last_err)
                    break  # fatal или retry исчерпан → следующая модель

    # 4. Локальный рубеж (если ещё не пробовали в local-first)
    if not try_local_first:
        ol = try_ollama(messages, _system_for(system, "gemma3:4b"))
        if ol is not None:
            logger.warning(
                "[%s] вся облачная цепочка недоступна (%s) → ответ выдан Ollama. Глубокая деградация.",
                label, last_err,
            )
            return LLMResult(ol, "ollama", degraded=True, error=last_err)

    # 5. Каскад: упало всё, включая Ollama → CRITICAL + сквозной алерт владельцу
    chain_repr = " → ".join(chain + ["ollama"])
    logger.critical(
        "[%s] КАСКАД: вся цепочка упала (%s). Последняя ошибка: %s. Нужно вмешательство.",
        label, chain_repr, last_err,
    )
    if owner_alert and _alert_once(f"{label}:{int(time.time() // 3600)}"):
        try:
            owner_alert(
                f"🔴 {label}: вся цепочка упала ({chain_repr}). "
                f"Последняя ошибка: {last_err}. Нужно вмешательство."
            )
        except Exception as e:  # noqa: BLE001 — алерт не должен ронять вызов
            logger.info("owner_alert провалился: %s", e)

    return LLMResult(_degraded_stub(stub_text), "none", degraded=True, error=last_err)
