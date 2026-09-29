import contextlib
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / 'naga_classic.py'
spec = importlib.util.spec_from_file_location('naga', SCRIPT_PATH)
naga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(naga)


class UsbSimulation:
    """A separately encoded USB response model; no physical device access."""
    def __init__(self, transaction=0x1f, vendor=0x1532, product=0x0093):
        self.transaction = transaction
        self.vendor, self.product = vendor, product
        self.dpi, self.poll = (800, 800), 500
        self.commands = []
        self.fail_poll_set = False
        self.drop_dpi_set = False
        self.corrupt_crc = False

    def ioctl(self, fd, command, buf, mutate=True):
        if command == 0x80084803:
            buf[:] = struct.pack('=IHH', 3, self.vendor, self.product)
            return 0
        if command == 0xc05b4806:
            assert len(buf) == 91 and buf[0] == 0
            request = bytes(buf[1:])
            self.commands.append(request)
            self.pending = request
            return 91
        if command != 0xc05b4807:
            raise AssertionError(f'Unexpected ioctl {command:x}')
        request = self.pending
        transaction, size, cls, cmd = request[1], request[5], request[6], request[7]
        status = 2 if transaction == self.transaction else 5
        args = bytearray(request[8:8 + size])
        if status == 2:
            if (cls, cmd) == (4, 0x85):
                args = bytearray([0]) + bytearray(struct.pack('>HH', *self.dpi)) + bytearray(2)
            elif (cls, cmd) == (0, 0x85):
                args = bytearray([{125: 8, 500: 2, 1000: 1}[self.poll]])
            elif (cls, cmd) == (4, 5):
                if not self.drop_dpi_set:
                    self.dpi = struct.unpack('>HH', args[1:5])
            elif (cls, cmd) == (0, 5):
                if self.fail_poll_set:
                    status = 5
                else:
                    self.poll = {8: 125, 2: 500, 1: 1000}[args[0]]
            else:
                raise AssertionError('Unexpected command')
        response = bytearray([status, transaction, 0, 0, 0, size, cls, cmd])
        response += args + bytearray(80 - len(args))
        checksum = 0
        for value in response[2:]:
            checksum ^= value
        response += bytes([checksum ^ int(self.corrupt_crc), 0])
        buf[:] = bytes([0]) + response
        return 91


@contextlib.contextmanager
def connected(sim):
    with patch.object(naga.os, 'open', return_value=99), \
         patch.object(naga.os, 'close'), \
         patch.object(naga.fcntl, 'flock'), \
         patch.object(naga.fcntl, 'ioctl', side_effect=sim.ioctl), \
         patch.object(naga.time, 'sleep'):
        mouse = naga.HidMouse('/dev/simulated-naga')
        try:
            yield mouse
        finally:
            mouse.close()


class Tests(unittest.TestCase):
    def test_known_request_vectors(self):
        self.assertEqual(naga.packet(0x1f, 0, 0x85, bytes(1)),
                         bytes([0, 31, 0, 0, 0, 1, 0, 133]) + bytes(80) + bytes([132, 0]))
        self.assertEqual(naga.packet(0x1f, 4, 0x85, bytes(7)),
                         bytes([0, 31, 0, 0, 0, 7, 4, 133]) + bytes(80) + bytes([134, 0]))
        self.assertEqual(naga.packet(0x1f, 4, 5, bytes([1, 6, 64, 6, 64, 0, 0])),
                         bytes([0, 31, 0, 0, 0, 7, 4, 5, 1, 6, 64, 6, 64, 0, 0]) + bytes(73) + bytes([7, 0]))
        self.assertEqual(naga.packet(0x1f, 0, 5, bytes([1])),
                         bytes([0, 31, 0, 0, 0, 1, 0, 5, 1]) + bytes(79) + bytes([5, 0]))

    def test_read_never_sends_set_commands(self):
        sim = UsbSimulation()
        with connected(sim) as mouse:
            self.assertEqual(naga.preflight(mouse.exchange), (31, ((800, 800), 500)))
        self.assertTrue(all(r[7] == 0x85 for r in sim.commands))

    def test_read_falls_back_to_known_trinity_identifier(self):
        sim = UsbSimulation(transaction=255)
        with connected(sim) as mouse:
            self.assertEqual(naga.preflight(mouse.exchange)[0], 255)
        self.assertEqual({r[1] for r in sim.commands}, {31, 255})
        self.assertTrue(all(r[7] == 0x85 for r in sim.commands))

    def test_wrong_device_is_never_sent_commands(self):
        sim = UsbSimulation(product=0x0067)
        with self.assertRaises(naga.MouseError):
            with connected(sim):
                pass
        self.assertEqual(sim.commands, [])

    def test_corrupted_responses_block_preflight(self):
        sim = UsbSimulation()
        sim.corrupt_crc = True
        with connected(sim) as mouse:
            with self.assertRaisesRegex(naga.MouseError, 'No settings-change'):
                naga.preflight(mouse.exchange)
        self.assertTrue(all(r[7] == 0x85 for r in sim.commands))

    def test_successful_change_uses_both_setters_and_reads_back(self):
        sim = UsbSimulation()
        with connected(sim) as mouse:
            tid, before = naga.preflight(mouse.exchange)
            after = naga.configure(mouse.exchange, tid, before, (1600, 1600), 1000)
        self.assertEqual(after, ((1600, 1600), 1000))
        writes = [(r[6], r[7]) for r in sim.commands if r[7] != 0x85]
        self.assertEqual(writes, [(4, 5), (0, 5)])

    def test_failed_dpi_readback_prevents_poll_write(self):
        sim = UsbSimulation()
        sim.drop_dpi_set = True
        with connected(sim) as mouse:
            tid, before = naga.preflight(mouse.exchange)
            with self.assertRaisesRegex(naga.MouseError, 'DPI change did not read back'):
                naga.configure(mouse.exchange, tid, before, (1600, 1600), 1000)
        self.assertFalse(any(r[6:8] == bytes([0, 5]) for r in sim.commands))

    def test_unchanged_values_do_not_send_setters(self):
        sim = UsbSimulation()
        with connected(sim) as mouse:
            tid, before = naga.preflight(mouse.exchange)
            self.assertEqual(naga.configure(mouse.exchange, tid, before, (800, 800), 500), before)
        self.assertTrue(all(r[7] == 0x85 for r in sim.commands))

    def test_only_documented_commands_are_allowed(self):
        with self.assertRaises(naga.MouseError):
            naga.packet(31, 0, 4, bytes([3, 0]))

    def test_dpi_validation_and_asymmetric_restore(self):
        self.assertEqual(naga.dpi_argument('1600'), (1600, 1600))
        self.assertEqual(naga.dpi_argument('800:1800'), (800, 1800))
        for value in ['0', '99', '16001', '1:2:3', 'hello']:
            with self.assertRaises(naga.argparse.ArgumentTypeError):
                naga.dpi_argument(value)

    def test_matching_command_required(self):
        request = naga.packet(31, 0, 0x85, bytes(1))
        response = bytearray(request)
        response[0] = 2
        response[6] = 4
        response[88] = naga.crc(response)
        with self.assertRaisesRegex(naga.MouseError, 'different command'):
            naga.checked_response(bytes([0]) + response, request)

    def test_discovery_selects_correct_id_and_interface(self):
        with tempfile.TemporaryDirectory() as root:
            entries = []
            for index, product, iface in [(0, 0x0093, 0), (1, 0x0093, 1), (2, 0x0067, 0)]:
                entry = Path(root) / 'class' / f'hidraw{index}'
                usb = Path(root) / f'usb-{index}'
                device = usb / 'hid'
                entry.mkdir(parents=True)
                device.mkdir(parents=True)
                (usb / 'bInterfaceNumber').write_text(f'{iface:02x}')
                (device / 'uevent').write_text(f'HID_ID=0003:00001532:{product:08X}\n')
                (entry / 'device').symlink_to(device)
                entries.append(entry)
            with patch.object(naga.Path, 'glob', return_value=iter(entries)):
                self.assertEqual(naga.locate_mouse(), Path('/dev/hidraw0'))

    def test_partial_failure_message_contains_restore_and_does_not_claim_success(self):
        sim = UsbSimulation()
        sim.fail_poll_set = True
        out, err = io.StringIO(), io.StringIO()
        with connected(sim) as mouse, \
             patch.object(naga, 'locate_mouse', return_value=Path('/dev/simulated-naga')), \
             patch.object(naga, 'HidMouse', return_value=mouse), \
             patch.object(naga.sys, 'argv', ['naga_classic.py', '--dpi', '1600', '--polling-rate', '1000']), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(naga.main(), 1)
        self.assertIn('--dpi 800:800 --polling-rate 500', out.getvalue())
        self.assertIn('Some settings may already have changed', err.getvalue())
        self.assertNotIn('Verified readback', out.getvalue())


if __name__ == '__main__':
    unittest.main()
