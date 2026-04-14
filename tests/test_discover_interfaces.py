import unittest
from unittest.mock import patch
from pathlib import Path

import dualwan_manager as dwm


class TestDiscoverInterfaces(unittest.TestCase):
    def test_extract_iface_from_filename(self):
        # Arrange: pretend the network directory exists and contains 10-eth0.network
        fake_dir = Path('/fake/systemd/network')
        test_paths = [Path('/etc/systemd/network/10-eth0.network')]

        with patch.object(dwm, 'NETWORK_DIR', fake_dir, create=True):
            with patch('pathlib.Path.exists', return_value=True):
                with patch('pathlib.Path.glob', return_value=test_paths):
                    # Act
                    ifaces = dwm.discover_interfaces()

        # Assert
        self.assertIn('eth0', ifaces)
        # and only once
        self.assertEqual(ifaces.count('eth0'), 1)


if __name__ == '__main__':
    unittest.main()
