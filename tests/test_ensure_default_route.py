import unittest
from unittest.mock import patch

import dualwan_manager as dwm


class TestEnsureDefaultRoute(unittest.TestCase):
    def test_command_without_gateway(self):
        iface = 'eth9'
        calls = []

        def fake_run_cmd(cmd, check: bool = False):
            calls.append(list(cmd))
            # Simulate `ip -4 route show dev <iface>` with no default route present
            if cmd == ['ip', '-4', 'route', 'show', 'dev', iface]:
                return 0, 'linkdown proto kernel\n', ''
            # Simulate success for the route replace command
            return 0, '', ''

        with patch.object(dwm, 'run_cmd', side_effect=fake_run_cmd):
            # Act: run with dry_run=False so it actually calls run_cmd for the replace
            dwm.ensure_default_route(iface, dry_run=False)

        # Assert: a call should be the replace default via dev only
        # Find the call that starts with ['ip','route','replace','default']
        replace_calls = [c for c in calls if c[:4] == ['ip', 'route', 'replace', 'default']]
        self.assertTrue(replace_calls, f"No replace default call found in calls: {calls}")
        # Expect exact command when gateway is missing
        self.assertIn(['ip', 'route', 'replace', 'default', 'dev', iface], replace_calls)


if __name__ == '__main__':
    unittest.main()
