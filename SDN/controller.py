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

TABLE_MISS = 0
LEARNED = 10
PROTECTED = 100
ALLOW = 200

datapaths = {}
mac_table = {}
allowed = set()
blocked = 0


def add_flow(dp, priority, match, actions):
    parser = dp.ofproto_parser
    instruction = parser.OFPInstructionActions(
        dp.ofproto.OFPIT_APPLY_ACTIONS, actions
    )
    message = parser.OFPFlowMod(
        datapath=dp,
        priority=priority,
        match=match,
        instructions=[instruction],
    )
    dp.send_msg(message)


def allow_client(dp, ip):
    parser = dp.ofproto_parser
    actions = [parser.OFPActionOutput(dp.ofproto.OFPP_NORMAL)]

    match = parser.OFPMatch(
        eth_type=0x0800,
        ip_proto=6,
        ipv4_src=ip,
        ipv4_dst=SERVER_IP,
        tcp_dst=TRANSFER_PORT,
    )
    add_flow(dp, ALLOW, match, actions)

    match = parser.OFPMatch(
        eth_type=0x0800,
        ip_proto=6,
        ipv4_src=SERVER_IP,
        ipv4_dst=ip,
        tcp_src=TRANSFER_PORT,
    )
    add_flow(dp, ALLOW, match, actions)


def remove_client(dp, ip):
    parser = dp.ofproto_parser
    ofproto = dp.ofproto

    match1 = parser.OFPMatch(
        eth_type=0x0800,
        ip_proto=6,
        ipv4_src=ip,
        ipv4_dst=SERVER_IP,
        tcp_dst=TRANSFER_PORT,
    )
    match2 = parser.OFPMatch(
        eth_type=0x0800,
        ip_proto=6,
        ipv4_src=SERVER_IP,
        ipv4_dst=ip,
        tcp_src=TRANSFER_PORT,
    )

    for match in (match1, match2):
        message = parser.OFPFlowMod(
            datapath=dp,
            command=ofproto.OFPFC_DELETE,
            match=match,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
        )
        dp.send_msg(message)


class ControlHandler(BaseHTTPRequestHandler):
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
                allow_client(dp, ip)
            print("granted", ip, flush=True)
            self.reply({"ok": True, "granted": ip})
        elif self.path == "/revoke":
            allowed.discard(ip)
            for dp in datapaths.values():
                remove_client(dp, ip)
            print("revoked", ip, flush=True)
            self.reply({"ok": True, "revoked": ip})
        else:
            self.reply({"ok": False, "error": "unknown path"}, 404)

    def log_message(self, *args):
        pass


class Firewall(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        server = ThreadingHTTPServer(("0.0.0.0", REST_PORT), ControlHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        print("control API on port", REST_PORT, flush=True)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_connected(self, ev):
        dp = ev.msg.datapath
        datapaths[dp.id] = dp
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        add_flow(
            dp,
            TABLE_MISS,
            parser.OFPMatch(),
            [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)],
        )

        match = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_dst=SERVER_IP,
            tcp_dst=TRANSFER_PORT,
        )
        add_flow(
            dp,
            PROTECTED,
            match,
            [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)],
        )

        for ip in allowed:
            allow_client(dp, ip)

        print("switch", dp.id, "connected", flush=True)

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

        if (ip_pkt and tcp_pkt and ip_pkt.dst == SERVER_IP
                and tcp_pkt.dst_port == TRANSFER_PORT):
            if ip_pkt.src in allowed:
                out_port = mac_table[dp.id].get(eth.dst, dp.ofproto.OFPP_FLOOD)
                self.send_out(dp, msg, in_port, out_port)
            else:
                blocked += 1
                print("blocked", ip_pkt.src, "to", SERVER_IP, TRANSFER_PORT, flush=True)
            return

        out_port = mac_table[dp.id].get(eth.dst)
        if out_port is None:
            out_port = dp.ofproto.OFPP_FLOOD
        else:
            parser = dp.ofproto_parser
            match = parser.OFPMatch(
                in_port=in_port,
                eth_src=eth.src,
                eth_dst=eth.dst,
            )
            add_flow(dp, LEARNED, match, [parser.OFPActionOutput(out_port)])

        self.send_out(dp, msg, in_port, out_port)

    def send_out(self, dp, msg, in_port, out_port):
        parser = dp.ofproto_parser
        data = None
        if msg.buffer_id == dp.ofproto.OFP_NO_BUFFER:
            data = msg.data
        message = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=[parser.OFPActionOutput(out_port)],
            data=data,
        )
        dp.send_msg(message)
