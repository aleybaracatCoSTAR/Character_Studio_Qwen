"""Preflight must permit real TCP restarts without replacing a live service."""
import socket
import unittest

from live_voice.deployment import check_ports


class PortPreflightTests(unittest.TestCase):
    def listener(self, host='127.0.0.1'):
        server=socket.socket()
        server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server.bind((host,0));server.listen(1);server.settimeout(1)
        self.addCleanup(server.close)
        return server

    def test_live_loopback_listener_is_not_replaced(self):
        server=self.listener();port=server.getsockname()[1]
        with self.assertRaisesRegex(ValueError,f'Port {port} is already in use'):
            check_ports([port])
        # Accept the preflight's closed diagnostic connection before checking
        # another client against this deliberately one-entry test backlog.
        probe_peer,_=server.accept();probe_peer.close()
        with socket.create_connection(('127.0.0.1',port),timeout=1) as client:
            peer,_=server.accept();peer.close()

    def test_live_wildcard_listener_is_not_replaced(self):
        server=self.listener('0.0.0.0');port=server.getsockname()[1]
        with self.assertRaises(ValueError):check_ports([port])

    def test_recently_closed_connection_does_not_prevent_server_restart(self):
        server=self.listener();port=server.getsockname()[1]
        with socket.create_connection(('127.0.0.1',port),timeout=1) as client:
            peer,_=server.accept();peer.settimeout(1)
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(1),b'')
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(peer.recv(1),b'')
            peer.close()
        server.close()
        # Establish the exact false positive from the previous plain-bind probe.
        with socket.socket() as plain:
            with self.assertRaises(OSError):plain.bind(('127.0.0.1',port))
        check_ports([port])
        # asyncio/Uvicorn's real reuse policy can now take the same port.
        with socket.socket() as restarted:
            restarted.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            restarted.bind(('127.0.0.1',port));restarted.listen(1)

    def test_free_port_probe_does_not_keep_the_port_open(self):
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
        check_ports([port])
        with socket.socket() as server:
            server.bind(('127.0.0.1',port));server.listen(1)


if __name__=='__main__':unittest.main()
