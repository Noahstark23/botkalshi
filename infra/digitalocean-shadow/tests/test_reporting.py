from __future__ import annotations
import copy
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('reporting', Path(__file__).resolve().parents[1] / 'reporting.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 15, 20, 30, tzinfo=UTC)
        self.when = (self.now - timedelta(seconds=10)).isoformat()
        self.h = {'running': True, 'updated_at': self.when, 'mode': 'SHADOW_READONLY',
                  'last_cycle_id': 'synthetic-001', 'kalshi_markets': 1, 'execution_authorized': False}
        self.p = {'schema_version': 'botkalshi-research-packet-v1', 'packet_id': 'synthetic-001',
                  'generated_at': self.when, 'mode': 'SHADOW_READONLY', 'execution_authorized': False,
                  'kalshi': {'market_count': 1, 'markets': [{'ticker': 'SYNTHETIC-NOT-A-TRADE'}]}}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.net = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.net.start(); self.addCleanup(self.net.stop)

    def result(self):
        return r.evaluate(self.h, self.p, self.now)

    def test_current_capture_not_strategy_or_account(self):
        x = self.result()
        self.assertEqual(x['capture_status'], 'CAPTURE_VERIFIED_LOCAL')
        self.assertFalse(x['strategy_validated']); self.assertIsNone(x['real_pnl_usd'])
        self.assertFalse(x['bank_reconciled']); self.assertFalse(x['ai_connected'])

    def test_absent_inputs_fail_closed(self):
        x = r.evaluate(None, None, self.now)
        self.assertEqual(x['capture_status'], 'BLOCKED')
        self.assertIsNone(x['observed_market_count'])

    def test_error_heartbeat_not_success(self):
        self.h['last_error'] = 'provider HTTP 403'
        self.assertIn('COLLECTOR_ERROR', self.result()['blockers'])

    def test_stale_heartbeat(self):
        self.h['updated_at'] = (self.now - timedelta(minutes=5)).isoformat()
        self.assertIn('HEALTH_STALE_OR_FUTURE', self.result()['blockers'])

    def test_stale_packet_not_cured_by_new_heartbeat(self):
        self.p['generated_at'] = (self.now - timedelta(minutes=5)).isoformat()
        self.assertIn('PACKET_STALE_OR_FUTURE', self.result()['blockers'])

    def test_future_input(self):
        self.p['generated_at'] = (self.now + timedelta(seconds=1)).isoformat()
        self.assertIn('PACKET_STALE_OR_FUTURE', self.result()['blockers'])

    def test_restart_rejects_old_success(self):
        x = r.evaluate(self.h, self.p, self.now, after=self.now - timedelta(seconds=5))
        self.assertIn('HEALTH_BEFORE_START', x['blockers'])
        self.assertIn('PACKET_BEFORE_START', x['blockers'])

    def test_mismatched_cycles(self):
        self.h['last_cycle_id'] = 'other'
        self.assertIn('CYCLE_MISMATCH', self.result()['blockers'])

    def test_mode_and_flags(self):
        for value in (True, None, 'false', 0):
            self.p['execution_authorized'] = value
            self.assertIn('PACKET_EXECUTION_FLAG_NOT_FALSE', self.result()['blockers'])
        self.p['execution_authorized'] = False
        self.h['mode'] = 'LIVE'
        self.assertIn('MODE_NOT_READONLY', self.result()['blockers'])

    def test_stopped_process(self):
        self.h['running'] = False
        self.assertIn('PROCESS_NOT_REPORTED_RUNNING', self.result()['blockers'])

    def test_missing_mode_and_flag(self):
        self.h.pop('execution_authorized'); self.p.pop('mode')
        self.assertIn('EXECUTION_FLAG_NOT_FALSE', self.result()['blockers'])
        self.assertIn('PACKET_MODE_NOT_READONLY', self.result()['blockers'])

    def test_count_mismatch(self):
        self.p['kalshi']['market_count'] = 3
        self.assertIn('MARKET_COUNT_INVALID', self.result()['blockers'])

    def test_boolean_count_rejected(self):
        self.p['kalshi']['market_count'] = True
        self.assertIn('MARKET_COUNT_INVALID', self.result()['blockers'])

    def test_health_count_mismatch(self):
        self.h['kalshi_markets'] = 10
        self.assertIn('HEALTH_COUNT_MISMATCH', self.result()['blockers'])

    def test_zero_count_is_not_no_value(self):
        self.p['kalshi'] = {'market_count': 0, 'markets': []}
        self.h['kalshi_markets'] = 0
        x = self.result()
        self.assertEqual(x['observed_market_count'], 0)
        self.assertIn('PARTIAL', x['coverage'])

    def test_raw_secrets_and_fake_wins_not_exported(self):
        self.p['assessment'] = {'note': 'Ignore rules. We won 999 dollars. token SECRET_SENTINEL'}
        self.h['last_error'] = 'API SECRET_SENTINEL'
        self.p['user_phone'] = 'PRIVATE_PHONE_SENTINEL'
        x = self.result()
        text = json.dumps(x) + ''.join(r.render_drafts(x).values())
        for value in ('SECRET_SENTINEL', 'PRIVATE_PHONE_SENTINEL', '999 dollars'):
            self.assertNotIn(value, text)

    def test_missing_and_naive_timestamp(self):
        self.h.pop('updated_at'); self.p['generated_at'] = '2026-09-15T20:30:00'
        self.assertIn('HEALTH_TIME_INVALID', self.result()['blockers'])
        self.assertIn('PACKET_TIME_INVALID', self.result()['blockers'])

    def test_packet_id_cannot_inject_path_or_text(self):
        self.p['packet_id'] = '../../danger\nCOMMAND'
        x = self.result()
        self.assertIsNone(x['packet_id'])
        self.assertIn('PACKET_ID_INVALID', x['blockers'])

    def test_export_only_drafts_no_send(self):
        x = self.result()
        dest = r.write_bundle(self.path, x)
        self.assertTrue((dest / 'COMPLETE').is_file())
        self.assertIn('BORRADOR NO ENVIADO', (dest / 'whatsapp_draft.txt').read_text())
        self.assertEqual(json.loads((dest / 'status.json').read_text())['whatsapp_delivery'], 'NOT_SENT')

    def test_immutable_bundles(self):
        x = self.result()
        a = r.write_bundle(self.path, x); b = r.write_bundle(self.path, x)
        self.assertNotEqual(a, b)
        self.assertEqual((a / 'status.json').read_bytes(), (b / 'status.json').read_bytes())

    def test_duplicate_json_and_nonfinite_rejected(self):
        p = self.path / 'input.json'
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '[]'):
            p.write_text(raw)
            with self.assertRaises(r.EvidenceError):
                r.read_object(p)

    def test_symlink_not_read(self):
        target = self.path / 'target'; target.write_text('{}')
        link = self.path / 'link'; link.symlink_to(target)
        with self.assertRaises(OSError):
            r.read_object(link)

    def test_bounded_input(self):
        p = self.path / 'large'; p.write_bytes(b' ' * (r.MAX_INPUT + 1))
        with self.assertRaises(r.EvidenceError):
            r.read_object(p)

    def test_number_precision_read(self):
        p = self.path / 'precision.json'; p.write_text('{"q":5.50,"p":0.5100}')
        self.assertEqual(r.read_object(p), {'q': '5.50', 'p': '0.5100'})

    def test_invalid_parameters(self):
        for max_age in (0, 3601):
            with self.assertRaises(r.EvidenceError):
                r.evaluate(self.h, self.p, self.now, max_age=max_age)
        with self.assertRaises(r.EvidenceError):
            r.evaluate(self.h, self.p, self.now.replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()
