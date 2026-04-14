import unittest
from unittest.mock import patch

from dualwan_manager import CloudflareClient


class TestZoneResolution(unittest.TestCase):
    def test_more_specific_zone_wins(self):
        cf = CloudflareClient(api_token='dummy')
        calls = []

        def fake_get_zone_id(name):
            calls.append(name)
            if name == 'sub.example.com':
                return 'zone-sub'
            if name == 'example.com':
                return 'zone-root'
            return None

        with patch.object(CloudflareClient, 'get_zone_id', side_effect=fake_get_zone_id):
            zone_name, zone_id = cf.find_best_zone_for_fqdn('host.sub.example.com')

        # Should choose the most specific zone available
        self.assertEqual(zone_name, 'sub.example.com')
        self.assertEqual(zone_id, 'zone-sub')
        # And ensure order tried is longest to shortest (no assertion on exact full list, but first call should be most specific)
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(calls[0], 'sub.example.com')

    def test_fallback_to_less_specific(self):
        cf = CloudflareClient(api_token='dummy')
        calls = []

        def fake_get_zone_id(name):
            calls.append(name)
            if name == 'sub.example.com':
                return None
            if name == 'example.com':
                return 'zone-root'
            return None

        with patch.object(CloudflareClient, 'get_zone_id', side_effect=fake_get_zone_id):
            zone_name, zone_id = cf.find_best_zone_for_fqdn('host.sub.example.com')

        self.assertEqual(zone_name, 'example.com')
        self.assertEqual(zone_id, 'zone-root')
        # Ensure it tried the more specific first, then fell back
        self.assertEqual(calls[:2], ['sub.example.com', 'example.com'])


if __name__ == '__main__':
    unittest.main()
