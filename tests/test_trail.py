import socket
import struct
import threading
import time

from rich.text import Text

from vectis import netprobe, trail

DNS_SRV, LB, CLIENT = "10.0.0.5", "10.0.0.6", "10.0.0.7"


def ev(source, line, ip, ts):
    return {"seq": 0, "ts": ts, "ip": ip, "source": source, "line": line}


def nginx(rid, status=200, upstream="10.0.0.3:3001", ustatus="200", uri="/", host="app.team1.test", client=CLIENT):
    return (f'rid={rid} client={client} host={host} method=GET uri="{uri}" status={status} '
            f'upstream="{upstream}" ustatus="{ustatus}" urt="0.001" rt=0.002 bytes=41')


def backend(rid, b="A"):
    return f"req rid={rid} backend={b} client={CLIENT} host=app.team1.test method=GET uri=/ status=200 ms=0.2"


def dns_pair(ts, serial=4, client=CLIENT, answer=LB):
    return [
        ev("dnsmasq", f"Nov 24 03:27:18 dnsmasq[174]: {serial} {client}/55656 query[A] app.team1.test from {client}", DNS_SRV, ts),
        ev("dnsmasq", f"Nov 24 03:27:18 dnsmasq[174]: {serial} {client}/55656 config app.team1.test is {answer}", DNS_SRV, ts),
    ]


def render(events, show_probes=False):
    v = trail.TrailView("team1.test", show_probes=show_probes)
    for e in events:
        v.feed(e)
    return Text.from_markup("\n".join(v.drain(force=True))).plain


def test_kv_parsing_handles_quoted_upstream_lists():
    kv = trail.parse_kv(nginx("abc", upstream="10.0.0.4:3002, 10.0.0.3:3001", ustatus="502, 200"))
    assert kv["upstream"] == "10.0.0.4:3002, 10.0.0.3:3001" and kv["ustatus"] == "502, 200"


def test_full_trail_dns_lb_backend():
    out = render(dns_pair(100.0) + [ev("nginx-access", nginx("r1"), LB, 100.1), ev("backend_a", backend("r1"), "10.0.0.3", 100.1)])
    assert "app.team1.test -> 10.0.0.6" in out and "10.0.0.5 answered from its own config" in out
    assert "10.0.0.3:3001 = Backend A: ok" in out
    assert "confirmed in its own log" in out


def test_dns_lookup_arriving_after_its_request_still_matches():
    out = render([ev("nginx-access", nginx("r1"), LB, 100.0)] + dns_pair(100.4))
    assert "app.team1.test -> 10.0.0.6" in out and "no lookup logged" not in out


def test_cached_dns_is_reported_not_invented():
    out = render([ev("nginx-access", nginx("r1"), LB, 500.0)])
    assert "no lookup logged" in out


def test_retry_shows_both_attempts():
    out = render([ev("nginx-access", nginx("r1", upstream="10.0.0.4:3002, 10.0.0.3:3001", ustatus="502, 200"), LB, 1.0),
                  ev("backend_a", backend("r1"), "10.0.0.3", 1.0)])
    assert "Backend B: failed" in out and "then retried" in out and "Backend A: ok" in out


def test_all_backends_down_is_a_502_with_clear_text():
    out = render([ev("nginx-access", nginx("r1", status=502, upstream="10.0.0.3:3001, team1_test_backends", ustatus="502, 502"), LB, 1.0)])
    assert "no usable backend" in out and "no healthy backend answered" in out and "502" in out


def test_request_by_ip_says_dns_not_used():
    out = render([ev("nginx-access", nginx("r1", host="10.0.0.6"), LB, 1.0)])
    assert "not used" in out


def test_health_probe_requests_hidden_unless_asked():
    e = [ev("nginx-access", nginx("r1", uri="/healthz"), LB, 1.0)]
    assert render(e) == "" and "/healthz" in render(e, show_probes=True)


def test_dns_machine_own_probe_queries_are_not_reported_as_orphans():
    out = render(dns_pair(1.0, client="127.0.0.1"))
    assert out == ""


def test_orphan_lookup_is_reported_when_no_request_follows():
    out = render(dns_pair(1.0))
    assert "no request reached the load balancer" in out


def test_events_come_out_in_time_order():
    v = trail.TrailView("team1.test")
    v.feed(ev("health", "DOWN backend_b :3002 process=NOT running: x", "10.0.0.4", 200.0))
    v.feed(ev("nginx-access", nginx("r1"), LB, 100.0))
    out = v.drain(force=True)
    assert "GET /" in out[0] and "DOWN backend_b" in out[1]


def test_nginx_errors_are_summarised_and_probe_noise_dropped():
    v = trail.TrailView("team1.test")
    refused = ('2026/11/24 03:28:45 [error] 623#623: *34 connect() failed (111: Connection refused) while connecting '
               'to upstream, client: 10.0.0.7, server: team1.test, request: "GET /api/status HTTP/1.1", '
               'upstream: "http://10.0.0.4:3002/api/status", host: "app.team1.test"')
    assert "backend 10.0.0.4:3002 refused the connection" in v._nginx_error(refused)
    assert v._nginx_error(refused.replace("GET /api/status", "GET /healthz")) is None


def test_waiting_for_log_lines_are_not_shown():
    assert render([ev("supervisor", "[waiting for supervisor.log to appear]", "10.0.0.3", 1.0)]) == ""


def test_dns_client_parses_a_real_packet_shape():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def answer():
        data, addr = srv.recvfrom(512)
        q = data[12:]
        resp = data[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + q
        resp += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 0, 4) + socket.inet_aton("10.0.0.6")
        srv.sendto(resp, addr)

    threading.Thread(target=answer, daemon=True).start()
    r = netprobe.dns_query_a("app.team1.test", "127.0.0.1", port=port, timeout=2)
    assert r.ok and r.addrs == ["10.0.0.6"] and r.ttl == 0
