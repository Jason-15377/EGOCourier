# -*- coding: utf-8 -*-
"""Orbbec SDK adapter tests that do not require a connected device."""
import ctypes
import os
import pathlib
import tempfile
import unittest

from ego_relay.orbbec_sdk import (
    FirmwareLogWriter,
    OrbbecSdkError,
    PACKET_BEGIN,
    PACKET_DATA,
    PACKET_END,
    find_sdk_dll,
)


class TestFirmwareLogWriter(unittest.TestCase):
    def test_assembles_callback_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = []
            writer = FirmwareLogWriter(tmp, lambda pct, name: progress.append((pct, name)))
            payload = ctypes.create_string_buffer(b"firmware")
            writer.packet(b"orbbec.log", PACKET_BEGIN, None, 0, 1, 0)
            writer.packet(b"orbbec.log", PACKET_DATA, payload, 4, 1, 0)
            writer.packet(b"orbbec.log", PACKET_END, ctypes.byref(payload, 4), 4, 1, 1)
            self.assertEqual(pathlib.Path(tmp, "orbbec.log").read_bytes(), b"firmware")
            self.assertEqual(progress[-1][0], 100)

    def test_rejects_path_and_records_callback_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = FirmwareLogWriter(tmp)
            writer.packet(b"../escape.log", PACKET_BEGIN, None, 0, 1, 0)
            self.assertIsInstance(writer.error, OrbbecSdkError)
            self.assertFalse(os.path.exists(os.path.join(tmp, "escape.log")))

    def test_default_sdk_package_is_discoverable_when_present(self):
        path = find_sdk_dll()
        if path:
            self.assertTrue(path.lower().endswith("orbbecsdk.dll"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
