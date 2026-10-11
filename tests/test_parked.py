"""Остановленный человеком сервис аудит не поднимает, упавший — чинит.

Регрессия на 08–10.10.2026: торговый отдел выключили ради денег, аудит Силли
прочитал REMOVED как падение и поднял Тилли обратно. Обратная сторона так же
важна: Railway ставит deploymentStopped каждой упавшей сборке, и если поверить
одному флагу, аудит перестанет чинить настоящие поломки.
"""

import unittest

from ai_office_shared.shared.parked import deploy_state


def resp(*nodes):
    return {"data": {"deployments": {"edges": [{"node": n} for n in nodes]}}}


class DeployStateTest(unittest.TestCase):
    def test_removed_is_parked(self):
        self.assertEqual(deploy_state(resp({"status": "REMOVED", "deploymentStopped": True})),
                         ("REMOVED", True))

    def test_stopped_success_is_parked(self):
        self.assertEqual(deploy_state(resp({"status": "SUCCESS", "deploymentStopped": True})),
                         ("SUCCESS", True))

    def test_failed_build_with_stop_flag_is_a_failure(self):
        # Так Railway помечает каждую упавшую сборку — это не «выключили».
        self.assertEqual(deploy_state(resp({"status": "FAILED", "deploymentStopped": True})),
                         ("FAILED", False))

    def test_crashed_is_a_failure_even_if_stopped(self):
        self.assertFalse(deploy_state(resp({"status": "CRASHED", "deploymentStopped": True}))[1])

    def test_running_is_not_parked(self):
        self.assertEqual(deploy_state(resp({"status": "SUCCESS", "deploymentStopped": False})),
                         ("SUCCESS", False))

    def test_no_deployments_is_not_parked(self):
        self.assertEqual(deploy_state(resp()), ("NO_DEPLOY", False))
        self.assertEqual(deploy_state({}), ("NO_DEPLOY", False))


if __name__ == "__main__":
    unittest.main()
