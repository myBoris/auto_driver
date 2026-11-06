"""Discovery offers identify the service without disclosing its access token."""

import json
import unittest

from center.config import CenterConfig
from center.discovery import DiscoveryProtocol


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, address):
        self.sent.append((data, address))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.protocol = DiscoveryProtocol(CenterConfig(
            token="private-discovery-test-token", port=8765,
            advertise_url="http://192.168.1.20:8765/",
        ))
        self.transport = FakeTransport()
        self.protocol.connection_made(self.transport)
        self.address = ("192.168.1.21", 45678)
        self.request = {
            "type": "discover", "service": "auto-driver-center",
            "protocol": 1, "nonce": "unique-discovery-request",
        }

    def test_offer_echoes_nonce_and_external_url_without_token(self):
        self.protocol.datagram_received(json.dumps(self.request).encode(), self.address)
        self.assertEqual(len(self.transport.sent), 1)
        payload, target = self.transport.sent[0]
        self.assertEqual(target, self.address)
        self.assertNotIn(b"private-discovery-test-token", payload)
        self.assertEqual(json.loads(payload), {
            "type": "offer", "service": "auto-driver-center", "protocol": 1,
            "nonce": self.request["nonce"], "port": 8765,
            "url": "http://192.168.1.20:8765",
        })

    def test_malformed_or_unrelated_datagrams_receive_no_offer(self):
        datagrams = [b"not json", b"\xff", b"[]", b"null", b"{}", b"x" * 1025]
        for field, value in (
            ("service", "other-service"), ("type", "offer"), ("protocol", 2),
            ("nonce", ""), ("nonce", 123), ("nonce", "x" * 129),
        ):
            datagrams.append(json.dumps({**self.request, field: value}).encode())
        for datagram in datagrams:
            with self.subTest(datagram=datagram[:100]):
                self.protocol.datagram_received(datagram, self.address)
        self.assertEqual(self.transport.sent, [])


if __name__ == "__main__":
    unittest.main()
