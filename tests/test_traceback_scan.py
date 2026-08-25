"""
Монитор Силли уничтожал трейсбек, по которому ставил диагноз — 25.08.2026.

За одну ночь в офис пришли две эскалации: villy-bot в 03:28 и office-dashboard
в 10:47 — с ОДНОЙ сигнатурой `1b21026f90825877` и fix_count=3 каждая. Обе
жаловались, что «traceback заканчивается на "Traceback (most recent call last):"
без тела исключения», и обе — что «исходник обрезан на 3000 символах».

Сигнатура выдала себя сама:

    md5("Traceback (most recent call last):")[:16] == "1b21026f90825877"

Хеш ровно одной строки — заголовка, одинакового у любого питон-процесса.
`strip_ignored_tracebacks` бережно собирал трейсбек блоком, а следующая строка
прогоняла результат через ПОСТРОЧНЫЙ фильтр ERROR_PATTERNS и выбрасывала тело.

Обе выдуманные по остатку причины проверены компилятором в тот же день:
villy-bot/bot.py:74 — `return r2.json().get("text", "").strip() or None`, а не
предсказанное «return r»; office-dashboard/main.py:91 — `out.sort(key=…,
reverse=True)`, закрытый, а не оборванный. Оба файла компилируются, pyflakes чист.

Тест №1 ПАДАЕТ на коде до фикса и проходит после — этим он и полезен.
Запуск: cd ai-office-shared && python3 -m pytest tests/test_traceback_scan.py -q
"""
import ast
import hashlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_office_shared.shared.file_window import around_lines  # noqa: E402
from ai_office_shared.shared.traceback_scan import (  # noqa: E402
    ERROR_PATTERNS, error_lines, frames, signature, signature_basis,
)

CODER = os.path.join(ROOT, "agents", "coder.py")

# Хеш из обеих эскалаций 25.08.2026 — константа инцидента, не выдумка теста.
INCIDENT_SIG = "1b21026f90825877"

# Логи в том виде, в каком их отдаёт Railway: одна строка стека — одна запись.
VILLY = [
    "INFO:aiogram.dispatcher:Start polling",
    "Traceback (most recent call last):",
    '  File "/app/bot.py", line 412, in transcribe_voice',
    "    return r2.json()",
    "aiogram.exceptions.TelegramBadRequest: Bad Request: file is too big",
]
DASHBOARD = [
    "Traceback (most recent call last):",
    '  File "/app/main.py", line 91, in api_tasks',
    '    out.sort(key=lambda x: x.get("created_at", 0), reverse=True)',
    "aiohttp.web_exceptions.HTTPNotFound: Not Found",
]
# Настоящий оборванный стек: процесс умер посреди печати (OOM/SIGKILL).
TRUNCATED = ["Traceback (most recent call last):"]

NOISE_THEN_REAL = [
    "Traceback (most recent call last):",
    '  File "/app/bot.py", line 9, in <module>',
    "    app.run_polling()",
    "telegram.error.Conflict: Conflict: terminated by other getUpdates request",
    "Traceback (most recent call last):",
    '  File "/app/bot.py", line 77, in handle',
    "    return data['text']",
    "KeyError: 'text'",
]
IGNORE = ["Conflict: terminated by other getUpdates", "TimedOut"]


def coder_ast():
    with open(CODER, encoding="utf-8") as f:
        src = f.read()
    return src, ast.parse(src)


def old_way(logs):
    """Как отбирал строки монитор до фикса — построчно поверх блочной чистки."""
    return [l for l in logs if any(p in l for p in ERROR_PATTERNS)]


def old_signature(error_logs):
    """Как считалась сигнатура до фикса (упрощённо до фолбэка, который и сработал)."""
    import re
    text = "\n".join(error_logs)
    exc = re.findall(r"\b([A-Za-z_]+(?:Error|Exception))\b", text)
    files = re.findall(r'File "[^"]*?([^"/\\]+\.py)"', text)
    msg = ""
    for line in reversed(error_logs):
        if exc and exc[-1] in line:
            msg = line
            break
    basis = "|".join([exc[-1] if exc else "", files[-1] if files else "",
                      re.sub(r"0x[0-9a-fA-F]+|\d+", "", msg).strip()]).strip("|")
    if not basis:
        basis = re.sub(r"0x[0-9a-fA-F]+|\d+", "", text)[:500]
    return hashlib.md5(basis.encode()).hexdigest()


class TestTheIncidentItself(unittest.TestCase):
    def test_old_path_really_produced_the_incident_hash(self):
        # Улика, а не пересказ: старый отбор на логе villy даёт ровно ту
        # сигнатуру, что пришла в офис.
        self.assertEqual(old_way(VILLY), ["Traceback (most recent call last):"])
        self.assertEqual(old_signature(old_way(VILLY))[:16], INCIDENT_SIG)

    def test_old_path_gave_two_different_services_the_same_hash(self):
        self.assertEqual(old_signature(old_way(VILLY))[:16],
                         old_signature(old_way(DASHBOARD))[:16])

    def test_new_path_keeps_the_whole_traceback(self):
        kept = error_lines(VILLY)
        self.assertIn('  File "/app/bot.py", line 412, in transcribe_voice', kept)
        self.assertIn("aiogram.exceptions.TelegramBadRequest: Bad Request: file is too big",
                      kept)

    def test_new_path_names_the_exception_the_old_regexp_could_not(self):
        # Именно TelegramBadRequest не проходил фильтр: класс не оканчивается
        # на Error/Exception перед двоеточием.
        basis = signature_basis(error_lines(VILLY))
        self.assertEqual(basis.exc, "TelegramBadRequest")
        self.assertEqual(basis.file, "bot.py")
        self.assertTrue(basis.complete)

    def test_the_file_frame_is_recoverable_again(self):
        # До фикса регексп `File "…"` не мог совпасть никогда: эти строки
        # выбрасывались раньше, чем до них доходили.
        self.assertEqual(frames(error_lines(VILLY)),
                         [("/app/bot.py", 412, "transcribe_voice")])

    def test_collision_between_the_two_services_is_closed(self):
        self.assertNotEqual(signature(error_lines(VILLY), scope="villy-svc"),
                            signature(error_lines(DASHBOARD), scope="dash-svc"))

    def test_neither_service_lands_on_the_incident_hash_any_more(self):
        for logs, scope in ((VILLY, "villy-svc"), (DASHBOARD, "dash-svc")):
            self.assertNotEqual(signature(error_lines(logs), scope=scope)[:16],
                                INCIDENT_SIG)


class TestHonestlyIncompleteStack(unittest.TestCase):
    """Оборванный стек бывает по-настоящему. Тогда это факт, а не повод молчать."""

    def test_bare_header_is_reported_incomplete(self):
        self.assertFalse(signature_basis(error_lines(TRUNCATED)).complete)

    def test_incomplete_signature_cannot_be_shared_between_services(self):
        # Иначе «та же сигнатура в 3+ сервисах» сочтёт это системным шумом,
        # сотрёт счётчики и выключит эскалацию офиса целиком.
        kept = error_lines(TRUNCATED)
        self.assertNotEqual(signature(kept, scope="villy-bot"),
                            signature(kept, scope="office-dashboard"))

    def test_a_full_stack_needs_no_scope_to_stay_distinct(self):
        # Полная улика различает сервисы сама; общий хеш у неё означает
        # настоящий общий баг, и это ценная информация — гасить её нельзя.
        self.assertEqual(signature(error_lines(VILLY), scope="a"),
                         signature(error_lines(VILLY), scope="b"))

    def test_an_unrelated_line_after_a_broken_stack_is_not_the_cause(self):
        logs = ["Traceback (most recent call last):",
                "INFO:aiogram:Update id=42 is handled"]
        basis = signature_basis(error_lines(logs))
        self.assertEqual(basis.exc, "")
        self.assertFalse(basis.complete)


class TestNoiseStillDropped(unittest.TestCase):
    """Регресс: блочная чистка шума 03.08.2026 обязана продолжать работать."""

    def test_conflict_block_is_dropped_whole_and_real_error_survives(self):
        kept = error_lines(NOISE_THEN_REAL, ignore=IGNORE)
        self.assertNotIn("telegram.error.Conflict: Conflict: terminated by other "
                         "getUpdates request", kept)
        self.assertIn("KeyError: 'text'", kept)
        self.assertEqual(signature_basis(kept).exc, "KeyError")

    def test_ignore_pattern_inside_a_frame_does_not_drop_a_real_error(self):
        # Паттерн, случайно попавший в кадр стека, не имеет права выбросить
        # настоящую ошибку: сверяется ОДНА строка исключения.
        logs = ["Traceback (most recent call last):",
                '  File "/app/net.py", line 3, in wait',
                "    log.debug('TimedOut once, retrying')",
                "KeyError: 'token'"]
        kept = error_lines(logs, ignore=["TimedOut"])
        self.assertIn("KeyError: 'token'", kept)

    def test_ignore_pattern_in_the_exception_line_does_drop_the_block(self):
        # Обратная сторона того же правила: решает строка исключения.
        logs = ["Traceback (most recent call last):",
                '  File "/app/net.py", line 3, in wait',
                "telegram.error.TimedOut: Timed out"]
        self.assertEqual(error_lines(logs, ignore=["TimedOut"]), [])

    def test_plain_error_lines_without_a_traceback_still_pass(self):
        # Синтетические строки из Redis-фолбэка (get_service_logs_via_redis).
        logs = ["ERROR 2026-08-25 villy: api_error",
                "INFO polling ok",
                "CRITICAL boom"]
        self.assertEqual(error_lines(logs), ["CRITICAL boom"])


class TestSourceWindow(unittest.TestCase):
    FILE = "\n".join(f"code_line_{i}" for i in range(1, 401))

    def test_window_shows_the_line_from_the_traceback(self):
        out = around_lines(self.FILE, [350], ctx_lines=3)
        self.assertIn("code_line_350", out)
        self.assertNotIn("code_line_1\n", out)   # начало файла не показываем

    def test_line_beyond_the_file_answers_honestly_with_nothing(self):
        # Пустой ответ читается как «лог и файл разошлись». Начало файла вместо
        # него — это и есть дефект, на который жаловались обе эскалации.
        self.assertEqual(around_lines(self.FILE, [99999]), "")

    def test_neighbouring_frames_merge_into_one_region(self):
        out = around_lines(self.FILE, [100, 102], ctx_lines=5)
        self.assertEqual(out.count("[строки"), 1)


class TestCoderNoLongerCutsBlind(unittest.TestCase):
    """agents/coder.py не импортируется — правки проверяем по AST."""

    def _fn(self, name):
        src, tree = coder_ast()
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                     and n.name == name), None)
        self.assertIsNotNone(node, f"{name} не найдена в coder.py")
        return ast.unparse(node)      # комментарии отброшены — остаётся код

    def test_escalation_windows_the_source_instead_of_slicing_3000(self):
        code = self._fn("_deep_diagnose_and_escalate")
        self.assertNotIn("[:3000]", code)
        self.assertIn("around_lines(", code)
        self.assertIn("frames(error_logs)", code)

    def test_escalation_gates_on_evidence_being_complete(self):
        code = self._fn("_deep_diagnose_and_escalate")
        self.assertIn("basis.complete", code)

    def test_monitor_no_longer_hashes_inline(self):
        code = self._fn("monitor_loop")
        self.assertNotIn("hashlib.md5", code)
        self.assertIn("error_lines(logs", code)


if __name__ == "__main__":
    unittest.main()
