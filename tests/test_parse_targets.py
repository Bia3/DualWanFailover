import unittest
from unittest.mock import patch, mock_open
from pathlib import Path

from dualwan_manager import parse_targets


class TestParseTargets(unittest.TestCase):
    def test_reject_dash_prefixed_targets(self):
        # Leading dash entries from env should be ignored
        res = parse_targets('-n, --help, 1.1.1.1, example.com, -badhost')
        self.assertIn('1.1.1.1', res)
        self.assertIn('example.com', res)
        self.assertNotIn('-n', res)
        self.assertNotIn('--help', res)
        self.assertNotIn('-badhost', res)

    def test_hosts_parsing_capped_and_filtered(self):
        # Build a fake /etc/hosts with a mix of addresses
        hosts_content = """
# Comment line
127.0.0.1 localhost
192.168.1.10 router.lan router
10.0.0.5 intranet.local intranet
203.0.113.5 example.com example
8.8.8.8 dns.google
198.51.100.7 many.hosts1.com alias1 alias2
1.2.3.4 host1.example.com h1
5.6.7.8 host2.example.com h2
9.10.11.12 host3.example.com h3
13.14.15.16 host4.example.com h4
17.18.19.20 host5.example.com h5
21.22.23.24 host6.example.com h6
25.26.27.28 host7.example.com h7
29.30.31.32 host8.example.com h8
33.34.35.36 host9.example.com h9
37.38.39.40 host10.example.com h10
41.42.43.44 host11.example.com h11
45.46.47.48 host12.example.com h12
49.50.51.52 host13.example.com h13
53.54.55.56 host14.example.com h14
57.58.59.60 host15.example.com h15
61.62.63.64 host16.example.com h16
        """.strip()

        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile('w+', delete=False) as tf:
            tf.write(hosts_content)
            tf.flush()
            temp_path = Path(tf.name)

        try:
            with patch('dualwan_manager.HOSTS_FILE', temp_path):
                res = parse_targets(None)
        finally:
            try:
                temp_path.unlink()
            except Exception:
                pass

        # Private/loopback should be filtered out (127.0.0.1, 192.168.1.10, 10.0.0.5)
        # Only global IPv4 lines contribute, preferring dotted hostnames when present.
        self.assertIn('dns.google', res)
        # Ensure no plain private IPs or local-only hosts included
        self.assertNotIn('localhost', res)
        self.assertNotIn('router.lan', res)
        self.assertNotIn('intranet.local', res)
        # Ensure cap to 20 items max
        self.assertLessEqual(len(res), 20)


if __name__ == '__main__':
    unittest.main()
