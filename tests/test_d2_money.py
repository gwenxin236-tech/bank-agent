"""D2: exact integer-cent presentation and business-boundary regressions."""
import ast
from decimal import Decimal
import inspect
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import banking_agent as ba


class IntegerMoneyTests(unittest.TestCase):
    def test_formats_cents_without_losing_small_or_negative_values(self):
        for cents, money, yuan in (
            (0, '¥0.00', '0 元'),
            (1, '¥0.01', '0.01 元'),
            (-1, '¥-0.01', '-0.01 元'),
            (29, '¥0.29', '0.29 元'),
            (-199, '¥-1.99', '-1.99 元'),
            (123456, '¥1,234.56', '1,234.56 元'),
            (-123400, '¥-1,234.00', '-1,234 元'),
            (ba.MAX_AMOUNT_CENTS, '¥10,000,000,000.00', '10,000,000,000 元'),
        ):
            with self.subTest(cents=cents):
                self.assertEqual(ba.money(cents), money)
                self.assertEqual(ba.yuan(cents), yuan)

    def test_formatting_keeps_every_cent_beyond_float_integer_precision(self):
        # Formatting is not a transfer authorization; the business cap stays enforced.
        for sign, prefix in ((1, ''), (-1, '-')):
            cents = sign * 9007199254740993
            self.assertEqual(ba.money(cents), '¥' + prefix + '90,071,992,547,409.93')
            self.assertEqual(ba.yuan(cents), prefix + '90,071,992,547,409.93 元')

    def test_formatters_reject_non_integer_cents_instead_of_coercing(self):
        for value in (True, False, 100.0, 0.29, Decimal('100'), '100', None):
            for formatter in (ba.money, ba.yuan):
                with self.subTest(value=repr(value), formatter=formatter.__name__):
                    with self.assertRaises(TypeError):
                        formatter(value)

    def test_formatters_do_not_use_float_conversion_or_true_division(self):
        for formatter in (ba.money, ba.yuan):
            tree = ast.parse(inspect.getsource(formatter))
            self.assertFalse(any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                                 for node in ast.walk(tree)), formatter.__name__)
            self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                 and node.func.id == 'float' for node in ast.walk(tree)), formatter.__name__)

    def test_input_conversion_preserves_cents_and_rejects_fractional_cents(self):
        for raw, expected in (('0.01', 1), ('0.29', 29), ('1,234.56', 123456),
                              ('10000000000.00', ba.MAX_AMOUNT_CENTS)):
            value, error = ba.IntentParser._amount(raw)
            self.assertIsNone(error)
            self.assertIs(type(value), int)
            self.assertEqual(value, expected)
        for raw in ('0.001', '1.999', 'NaN', '1e3', '-0.01', '10000000000.01'):
            value, error = ba.IntentParser._amount(raw)
            self.assertIsNone(value)
            self.assertTrue(error)

    def test_agent_transfer_and_ledger_keep_integer_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = ba.AgentRuntime(db_path=Path(tmp) / 'money.sqlite3', offline=True)
            try:
                before = rt.bank.balance().data['available_cents']
                response = rt.chat('给妈妈转 0.29 元')
                plan = response['plan']
                self.assertIs(type(plan['entities']['amount_cents']), int)
                self.assertEqual(plan['entities']['amount_cents'], 29)
                self.assertIn('0.29 元', response['plan_view']['summary'])
                result = rt.confirm(plan['plan_id'], plan['consent_token'])
                self.assertEqual(result['result']['status'], 'SUCCESS')
                after = rt.bank.balance().data['available_cents']
                self.assertIs(type(after), int)
                self.assertEqual(after, before - 29)
                row = rt.bank.db.execute('SELECT amount_cents, status FROM core_transactions').fetchone()
                self.assertIs(type(row['amount_cents']), int)
                self.assertEqual(row['amount_cents'], 29)
                self.assertEqual(row['status'], 'succeeded')
            finally:
                rt.bank.close()
                rt.audit.db.close()


if __name__ == '__main__':
    unittest.main()
