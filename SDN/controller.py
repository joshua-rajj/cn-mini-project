"""Ryu controller for the file sharing project.

It does two things:

1. Acts as a normal learning switch, so ARP, ping and the web dashboards work.
2. Blocks traffic going to the file server's TCP port (9000) unless that
   client has logged in. Logging in adds the client's IP to `allowed` and
   installs an OpenFlow rule for it. Logging out removes it again.

The Flask server controls this over a small HTTP API on 127.0.0.1:8080:

    POST /grant   {"ip": "10.0.0.11"}
    POST /revoke  {"ip": "10.0.0.11"}
    GET  /state

Run with:
    envs/ryu/bin/ryu-manager SDN/controller.py
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import ethernet, ipv4, packet, tcp
from ryu.ofproto import ofproto_v1_3

SERVER_IP = os.environ.get("SERVER_IP", "10.0.0.1")
TRANSFER_PORT = int(os.environ.get("TRANSFER_PORT", "9000"))
REST_PORT = int(os.environ.get("REST_PORT", "8080"))

# Flow priorities. Higher numbers win.
TABLE_MISS = 0
LEARNED = 10
PROTECTED = 100   # send server:9000 traffic to the controller
ALLOW = 200       # logged-in client, let it through

datapaths = {}    # switch id -> datapath
mac_table = {}    # switch id -> {mac address: port}
allowed = set()   # IPs that may use the file server
blocked = 0       # how many packets we refused


def log(message):
    print("[controller] " + message, flush=True)


def add_flow(dp, priority, match, actions):
    parser = dp.ofproto_parser
    instructions = [
        parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS, actions)
    ]
    dp.send_msg(parser.OFPFlowMod(
        datapath=dp, priority=priority, match=match, instructions=instructions))


def install_allow(dp, ip):
    """Let one client use the file server, in both directions."""
    parser = dp.ofproto_parser
    out = [parser.OFPActionOutput(dp.ofproto.OFPP_NORMAL)]

    # client -> server:9000
    match = parser.OFPMatch(eth_type=0x0800, ip_proto=6,
                            ipv4_src=ip, ipv4_dst=SERVER_IP,
                            tcp_dst=TRANSFER_PORT)
    add_flow(dp, ALLOW, match, out)

    # server:9000 -> client (the replies)
    match = parser.OFPMatch(eth_type=0x0800, ip_proto=6,
                            ipv4_src=SERVER_IP, ipv4_dst=ip,
                            tcp_src=TRANSFER_PORT)
    add_flow(dp, ALLOW, match, out)


def remove_allow(dp, ip):
    parser = dp.ofproto_parser
    ofproto = dp.ofproto
    for match in (
        parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=ip,
                        ipv4_dst=SERVER_IP, tcp_dst=TRANSFER_PORT),
        parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=SERVER_IP,
                        ipv4_dst=ip, tcp_src=TRANSFER_PORT),
    ):
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofproto.OFPFC_DELETE, match=match,
            out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY))


class ControlHandler(BaseHTTPRequestHandler):
    """Tiny HTTP API used by the Flask server to grant/revoke access."""

    def reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/state":
            self.reply({"allowed": sorted(allowed), "blocked": blocked})
        else:
            self.reply({"ok": False, "error": "unknown path"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        ip = data.get("ip")

        if not ip:
            self.reply({"ok": False, "error": "missing ip"}, 400)
        elif self.path == "/grant":
            allowed.add(ip)
            for dp in datapaths.values():
                install_allow(dp, ip)
            log("granted " + ip)
            self.reply({"ok": True, "granted": ip})
        elif self.path == "/revoke":
            allowed.discard(ip)
            for dp in datapaths.values():
                remove_allow(dp, ip)
            log("revoked " + ip)
            self.reply({"ok": True, "revoked": ip})
        else:
            self.reply({"ok": False, "error": "unknown path"}, 404)

    def log_message(self, *args):
        pass  # keep the console quiet


class Firewall(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", REST_PORT), ControlHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log("control API on 127.0.0.1:%d" % REST_PORT)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_connected(self, ev):
        dp = ev.msg.datapath
        datapaths[dp.id] = dp
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        # Unknown packets come to the controller.
        add_flow(dp, TABLE_MISS, parser.OFPMatch(),
                 [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                         ofproto.OFPCML_NO_BUFFER)])

        # Server traffic always comes to the controller first, so the learned
        # MAC rules above can never skip the login check.
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=6,
                                ipv4_dst=SERVER_IP, tcp_dst=TRANSFER_PORT)
        add_flow(dp, PROTECTED, match,
                 [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                         ofproto.OFPCML_NO_BUFFER)])

        # If clients logged in before the switch connected, add them now.
        for ip in allowed:
            install_allow(dp, ip)

        log("switch %s connected" % dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        global blocked

        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        mac_table.setdefault(dp.id, {})[eth.src] = in_port

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)

        # Traffic for the protected file server.
        if (ip_pkt and tcp_pkt and ip_pkt.dst == SERVER_IP
                and tcp_pkt.dst_port == TRANSFER_PORT):
            if ip_pkt.src in allowed:
                out_port = mac_table[dp.id].get(eth.dst, dp.ofproto.OFPP_FLOOD)
                self.send_out(dp, msg, in_port, out_port)
            else:
                blocked += 1
                log("blocked %s -> %s:%d" % (ip_pkt.src, SERVER_IP, TRANSFER_PORT))
            return

        # Normal learning switch.
        out_port = mac_table[dp.id].get(eth.dst)
        if out_port is None:
            out_port = dp.ofproto.OFPP_FLOOD
        else:
            parser = dp.ofproto_parser
            match = parser.OFPMatch(in_port=in_port, eth_src=eth.src,
                                    eth_dst=eth.dst)
            add_flow(dp, LEARNED, match, [parser.OFPActionOutput(out_port)])

        self.send_out(dp, msg, in_port, out_port)

    def send_out(self, dp, msg, in_port, out_port):
        parser = dp.ofproto_parser
        data = None
        if msg.buffer_id == dp.ofproto.OFP_NO_BUFFER:
            data = msg.data
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
            actions=[parser.OFPActionOutput(out_port)], data=data))
