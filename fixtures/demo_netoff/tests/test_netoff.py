import socket


def test_network_is_off():
    s = socket.socket()
    s.settimeout(2.0)
    try:
        s.connect(('93.184.216.34', 80))
        assert False, 'network connect unexpectedly succeeded'
    except OSError:
        assert True
    finally:
        s.close()
