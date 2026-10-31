import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba


class BirthdayLinkageTests(unittest.TestCase):
    def setUp(self):
        self.old_db = ba.DB_PATH
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        ba.DB_PATH = Path(path)
        self.runtime = ba.AgentRuntime()

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        try:
            self.runtime.audit.db.close()
        except Exception:
            pass
        try:
            os.unlink(ba.DB_PATH)
        except OSError:
            pass
        ba.DB_PATH = self.old_db

    def _birthday(self, days_from_today=3):
        birthday = date.fromisoformat(ba.today_key()) + timedelta(days=days_from_today)
        return birthday, f"{birthday.month}月{birthday.day}日"

    def _create_task(self, birthday_text):
        planned = self.runtime.chat(f"我爱人生日快到了，生日是{birthday_text}")
        self.assertEqual(planned["plan"]["intent"], "birthday_linkage")
        self.assertTrue(planned["plan"]["requires_confirmation"])
        confirmed = self.runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])
        self.assertEqual(confirmed["result"]["status"], "SUCCESS")
        return confirmed

    def test_create_runs_lock_and_persists_delayed_dag(self):
        _birthday, birthday_text = self._birthday()
        before = self.runtime.bank.balance().data["available_cents"]
        result = self._create_task(birthday_text)
        task_id = result["result"]["results"][0]["data"]["task_id"]

        self.assertEqual(self.runtime.bank.balance().data["available_cents"], before - 100000)
        task = self.runtime.get_linkage_task(task_id)
        self.assertEqual(task["status"], "ACTIVE")
        self.assertEqual([node["status"] for node in task["nodes"]], ["COMPLETED", "PENDING", "PENDING"])
        self.assertEqual(task["nodes"][1]["due_at"][:10], (date.fromisoformat(ba.today_key()) + timedelta(days=1)).isoformat())
        self.assertTrue(any(event["event_type"] == "linkage.created" for event in self.runtime.audit.list(100)))

    def test_restart_recovery_executes_due_nodes_and_is_idempotent(self):
        birthday, birthday_text = self._birthday()
        result = self._create_task(birthday_text)
        task_id = result["result"]["results"][0]["data"]["task_id"]
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        self.runtime.audit.db.close()

        restarted = ba.AgentRuntime()
        try:
            first_recovery = restarted.restore_scheduled_tasks(
                now=f"{(birthday - timedelta(days=2)).isoformat()}T12:00:00+00:00"
            )
            self.assertIn(task_id, first_recovery)
            task = restarted.get_linkage_task(task_id)
            self.assertEqual(task["nodes"][1]["status"], "COMPLETED")
            self.assertEqual(task["nodes"][2]["status"], "PENDING")
            self.assertEqual(len(restarted.bank.linkage_orders), 1)
            self.assertEqual(restarted.bank.linkage_orders[0]["kind"], "flowers")

            restarted.restore_scheduled_tasks(
                now=f"{(birthday - timedelta(days=2)).isoformat()}T12:00:00+00:00"
            )
            self.assertEqual(len(restarted.bank.linkage_orders), 1)
            events = restarted.audit.list(200)
            self.assertTrue(any(event["event_type"] == "linkage.node.completed" and event["payload"]["node_id"] == "B" for event in events))

            restarted.restore_scheduled_tasks(now=f"{(birthday - timedelta(days=1)).isoformat()}T12:00:00+00:00")
            task = restarted.get_linkage_task(task_id)
            self.assertEqual(task["nodes"][2]["status"], "COMPLETED")
            self.assertEqual({order["kind"] for order in restarted.bank.linkage_orders}, {"flowers", "cake"})
        finally:
            restarted.recovery_scheduler.stop()
            restarted.bank.close()
            restarted.audit.db.close()

    def test_cancel_compensates_executed_lock_and_cancels_pending_nodes(self):
        _birthday, birthday_text = self._birthday()
        before = self.runtime.bank.balance().data["available_cents"]
        result = self._create_task(birthday_text)
        task_id = result["result"]["results"][0]["data"]["task_id"]

        cancelled = self.runtime.cancel_linkage_task(task_id)
        self.assertTrue(cancelled["ok"])
        self.assertEqual(self.runtime.bank.balance().data["available_cents"], before)
        task = self.runtime.get_linkage_task(task_id)
        self.assertEqual(task["status"], "CANCELLED")
        self.assertEqual(task["nodes"][0]["status"], "COMPENSATED")
        self.assertEqual(task["nodes"][1]["status"], "CANCELLED")
        self.assertEqual(task["nodes"][2]["status"], "CANCELLED")
        self.assertTrue(any(event["event_type"] == "linkage.compensation.completed" for event in self.runtime.audit.list(100)))

    def test_custom_offsets_are_confirmed_and_persisted_independently(self):
        birthday, text = self._birthday(20)
        before = self.runtime.bank.balance().data['available_cents']
        planned = self.runtime.chat(f'我爱人生日是{text}，鲜花提前五天，蛋糕提前3天')
        self.assertEqual(planned['plan']['entities']['flowers_days_before'], 5)
        self.assertEqual(planned['plan']['entities']['cake_days_before'], 3)
        self.assertIn('鲜花提前5天', str(planned['plan_view']))
        self.assertIn('蛋糕提前3天', str(planned['plan_view']))
        self.assertEqual(self.runtime.bank.balance().data['available_cents'], before)
        confirmed = self.runtime.confirm(planned['plan']['plan_id'], planned['plan']['consent_token'])
        task = self.runtime.get_linkage_task(confirmed['result']['results'][0]['data']['task_id'])
        self.assertEqual(task['nodes'][1]['due_at'][:10], (birthday - timedelta(days=5)).isoformat())
        self.assertEqual(task['nodes'][2]['due_at'][:10], (birthday - timedelta(days=3)).isoformat())
        self.assertEqual(task['flowers_days_before'], 5)
        self.assertEqual(task['cake_days_before'], 3)

    def test_offset_before_item_and_same_day_are_supported(self):
        _, text = self._birthday(20)
        plan = self.runtime.chat(f'我爱人生日是{text}，提前4天订花，蛋糕当天订')['plan']
        self.assertEqual(plan['entities']['flowers_days_before'], 4)
        self.assertEqual(plan['entities']['cake_days_before'], 0)

    def test_ambiguous_invalid_or_elapsed_schedule_never_locks_money(self):
        _, text = self._birthday(20)
        cases = [f'我爱人生日是{text}，花提前-2天',
                 f'我爱人生日是{text}，蛋糕提前1.5天',
                 f'我爱人生日是{text}，花提前366天',
                 f'我爱人生日是{text}，花提前三天，花提前四天',
                 f'我爱人生日是{text}，礼物提前3天',
                 f'我爱人生日是{text}，花提前一周',
                 '我爱人生日是2月30日', '我爱人生日快到了']
        _, tomorrow = self._birthday(1)
        cases.append(f'我爱人生日是{tomorrow}，花提前3天')
        before = self.runtime.bank.balance().data['available_cents']
        for text in cases:
            with self.subTest(text=text):
                response = self.runtime.chat(text)
                self.assertTrue(response.get('blocked'), response)
                self.assertNotIn('result', response)
                p = response['plan']
                self.runtime.confirm(p['plan_id'], p['consent_token'])
                self.assertEqual(self.runtime.bank.balance().data['available_cents'], before)

    def test_tool_validates_offsets_even_without_parser(self):
        birthday, _ = self._birthday(20)
        before = self.runtime.bank.balance().data['available_cents']
        for offset in [-1, True, 1.5, 366, '3']:
            result = self.runtime.create_birthday_linkage(birthday.isoformat(), 'demo-user', 'test',
                                                         flowers_days_before=offset)
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, 'INVALID_BIRTHDAY_SCHEDULE')
        self.assertEqual(self.runtime.bank.balance().data['available_cents'], before)

    def test_iso_birthday_keeps_explicit_year(self):
        birthday, _ = self._birthday(20)
        self.assertEqual(self.runtime.parser._birthday_date(f'生日是{birthday.isoformat()}'), birthday.isoformat())
        self.assertEqual(self.runtime.parser._birthday_date('生日是2020-11-05'), '2020-11-05')


if __name__ == "__main__":
    unittest.main(verbosity=2)
