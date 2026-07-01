# Вставить в AGENTIC_SYSTEM prompt — в секцию с описанием доступных действий:
RAILWAY_LOGS_ACTION_DOC = '''
- {"action":"railway_logs","service":"<repo-имя>","filter":"<подстрока или пустая строка>","limit":50}
  Получить последние логи сервиса из Railway. service — одно из: billy-bot, dilly-bot, filly-bot,
  gosling-bot, kriss-bot, logger-bot, mama-bot, milly-bot, office-dashboard, pilly-bot,
  prophet-bot, tilly-bot, tilly-trader, villy-bot.
  filter — подстрока для фильтрации строк (пустая = без фильтра). limit — макс строк (дефолт 50).
'''