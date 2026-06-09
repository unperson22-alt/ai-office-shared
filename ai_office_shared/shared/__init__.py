"""ai_office_shared.shared — общая библиотека для всех ботов офиса.

Доступные модули:
    logging      — log_event()
    redis_helpers — redis_get_history, redis_save_history, redis_get_notes, redis_add_note
    tasks        — auto_extract_interests, schedule_loop, ...
    routing      — forward_to_filly, make_reply_handler, is_routed
    quality      — реакции 👍/👎
    ollama       — try_ollama(), OllamaResult (local LLM fallback)
    web_search   — web_search(), web_search_text()
    impact_client — get_campaigns(), get_ads(), get_tracking_link()
    voice        — transcribe_voice(file_url) → (text, error)
    office       — OFFICE_AGENTS, call_office(), parse_office_tag()
    crypto       — get_price(), get_prices(), get_prices_text()
    currency     — get_rate(), get_rates(), get_rates_text()
    wiki         — wiki_summary(), wiki_search(), wiki_text()
"""
# Импорты по необходимости — from ai_office_shared.shared.X import Y
