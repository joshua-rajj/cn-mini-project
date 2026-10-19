"""Ryu controller for Project 14 -- Concurrent File Sharing System.

Responsibilities
----------------
1. **L2 learning switch** so ARP/ICMP/normal traffic works (table-miss ->
   controller, learn source MAC, install forwarding).
2. **Policy enforcement for the protected transfer service** (TCP 9000 on the
   server host):
     * any packet addressed to ``server:9000`` that has no active grant is
       *dropped* at the controller (and counted),
     * a successful authentication installs a narrow, cookie-tagged allow rule
       for that client host so its transfer traffic is forwarded in the data
       plane,
     * logout/revocation deletes the grant's flows by cookie, so new
       connections are blocked again.

REST control plane (trusted host only -- bind to 127.0.0.1 by default):
    POST /policy/grant    {"src_ip","dst_ip","dport"}
    POST /policy/revoke   {"src_ip","dst_ip","dport"}
    GET  /policy/state
    GET  /policy/stats
    GET  /flows

Run:
    ryu-manager --ofp-tcp-listen-port 6653 SDN/controller/ryu_app.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.app.wsgi import WSGIApplication
from ryu.lib.packet import ethernet, ipv4, packet, tcp
from ryu.ofproto import ofproto_v1_3

from policy import PolicyEngine          # noqa: E402  (path set above)
from rest import PolicyRest             # noqa: E402

# --- policy tuning -----------------------------------------------------------
SERVER_IP = os.environ.get("SERVER_IP", "10.0.0.1")
TRANSFER_PORT = int(os.environ.get("TRANSFER_PORT", "9000"))

PRIORITY_TABLE_MISS = 0
PRIORITY_LEARNED = 10
PRIORITY_PROTECTED = 100     # protected traffic always goes to the controller,
                             # so priority-10 learned flows can never bypass policy
PRIORITY_ALLOW = 200         # beats everything; grants the protected flow

ETH_TYPE_IP = 0x0800


class PolicySwitch(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.policy = PolicyEngine()
        self.counters = {"allowed": 0, "denied": 0}
        self.flow_snapshots = {}

        kwargs["wsgi"].register(PolicyRest, {"app": self})

    # -- switch lifecycle ----------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("switch %s connected", dp.id)
        elif ev.state == "dead" or dp.id in self.datapaths:
            self.datapaths.pop(dp.id, None)
            self.logger.info("switch %s disconnected", dp.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        self._install_table_miss(dp)
        self._install_protected_rule(dp)

    def _install_table_miss(self, dp):
        ofproto, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(dp, PRIORITY_TABLE_MISS, match, actions)
        self.logger.info("switch %s: table-miss installed", dp.id)

    def _install_protected_rule(self, dp):
        """Send all client->server:9000 traffic to the controller.

        Priority 100 beats the priority-10 learned flows, so MAC learning cannot
        accidentally forward protected traffic past the policy check.
        """
        ofproto, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch(eth_type=ETH_TYPE_IP, ip_proto=6,
                                ipv4_dst=SERVER_IP, tcp_dst=TRANSFER_PORT)
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(dp, PRIORITY_PROTECTED, match, actions)
        self.logger.info("switch %s: protected rule (tcp dport %s) installed",
                         dp.id, TRANSFER_PORT)

    def _add_flow(self, dp, priority, match, actions, cookie=0,
                  idle_timeout=0, hard_timeout=0):
        ofproto, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(
            dp, cookie=cookie, priority=priority, match=match,
            instructions=inst, idle_timeout=idle_timeout,
            hard_timeout=hard_timeout))

    # -- dataplane -----------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofproto, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        self.mac_to_port.setdefault(dp.id, {})
        self.mac_to_port[dp.id][eth.src] = in_port

        ip = pkt.get_protocol(ipv4.ipv4)
        tcph = pkt.get_protocol(tcp.tcp)

        # Protected service: client -> server:9000/tcp.
        if (ip is not None and tcph is not None
                and ip.dst == SERVER_IP and tcph.dst_port == TRANSFER_PORT):
            self._handle_protected(dp, msg, eth, ip, tcph, in_port)
            return

        self._learn_and_forward(dp, msg, eth, in_port)

    def _handle_protected(self, dp, msg, eth, ip, tcph, in_port):
        if self.policy.is_allowed(ip.src, ip.dst, tcph.dst_port):
            self.counters["allowed"] += 1
            self.logger.info("ALLOW %s -> %s:%s", ip.src, ip.dst, tcph.dst_port)
            out_port = self.mac_to_port.get(dp.id, {}).get(eth.dst, dp.ofproto.OFPP_FLOOD)
            self._forward(dp, msg, in_port,
                          [dp.ofproto_parser.OFPActionOutput(out_port)])
        else:
            self.counters["denied"] += 1
            self.logger.warning("DENY  %s -> %s:%s (no grant)",
                                ip.src, ip.dst, tcph.dst_port)
            # drop: install no flow and forward nothing

    def _learn_and_forward(self, dp, msg, eth, in_port):
        ofproto, parser = dp.ofproto, dp.ofproto_parser
        if eth.dst in self.mac_to_port[dp.id]:
            out_port = self.mac_to_port[dp.id][eth.dst]
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst)
            self._add_flow(dp, PRIORITY_LEARNED, match, actions)
        else:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        self._forward(dp, msg, in_port, actions)

    def _forward(self, dp, msg, in_port, actions=None):
        ofproto, parser = dp.ofproto, dp.ofproto_parser
        if actions is None:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        if msg.buffer_id != ofproto.OFP_NO_BUFFER:
            dp.send_msg(parser.OFPPacketOut(
                dp, buffer_id=msg.buffer_id, in_port=in_port, actions=actions))
        else:
            dp.send_msg(parser.OFPPacketOut(
                dp, buffer_id=ofproto.OFP_NO_BUFFER, in_port=in_port,
                actions=actions, data=msg.data))

    # -- policy operations (called from REST) --------------------------------
    def grant_policy(self, src_ip, dst_ip, dport):
        dport = int(dport)
        grant, created = self.policy.grant(src_ip, dst_ip, dport)
        applied = 0
        for dp in self.datapaths.values():
            self._install_allow(dp, grant)
            applied += 1
        return {"ok": True, "created": created, "applied_switches": applied,
                "grant": grant.as_dict()}

    def revoke_policy(self, src_ip, dst_ip, dport):
        dport = int(dport)
        grant = self.policy.revoke(src_ip, dst_ip, dport)
        if grant is None:
            return {"ok": True, "removed": False}
        for dp in self.datapaths.values():
            self._delete_flows(dp, grant.cookie)
        return {"ok": True, "removed": True, "cookie": grant.cookie}

    def _install_allow(self, dp, grant):
        parser = dp.ofproto_parser
        # client -> server:9000
        m1 = parser.OFPMatch(eth_type=ETH_TYPE_IP, ip_proto=6,
                             ipv4_src=grant.src_ip, ipv4_dst=grant.dst_ip,
                             tcp_dst=grant.dport)
        a1 = [parser.OFPActionOutput(dp.ofproto.OFPP_NORMAL)]
        self._add_flow(dp, PRIORITY_ALLOW, m1, a1, cookie=grant.cookie,
                       hard_timeout=int(os.environ.get("GRANT_TIMEOUT", "0")))
        # server:9000 -> client (reply direction)
        m2 = parser.OFPMatch(eth_type=ETH_TYPE_IP, ip_proto=6,
                             ipv4_src=grant.dst_ip, ipv4_dst=grant.src_ip,
                             tcp_src=grant.dport)
        self._add_flow(dp, PRIORITY_ALLOW, m2, a1, cookie=grant.cookie,
                       hard_timeout=int(os.environ.get("GRANT_TIMEOUT", "0")))

    def _delete_flows(self, dp, cookie):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        dp.send_msg(parser.OFPFlowMod(
            dp, command=ofproto.OFPFC_DELETE, out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY, cookie=cookie, cookie_mask=0xFFFFFFFF,
            match=parser.OFPMatch(), priority=0))

    def request_flow_stats(self):
        for dp in self.datapaths.values():
            parser = dp.ofproto_parser
            req = parser.OFPFlowStatsRequest(dp)
            dp.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply(self, ev):
        dp = ev.msg.datapath
        stats = []
        for stat in ev.msg.body:
            stats.append({
                "priority": stat.priority,
                "match": str(stat.match),
                "packets": stat.packet_count,
                "bytes": stat.byte_count,
                "cookie": stat.cookie,
            })
        self.flow_snapshots[dp.id] = stats
