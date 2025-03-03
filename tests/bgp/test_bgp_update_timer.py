"""Check how fast FRR or QUAGGA will send updates to neighbors."""
import contextlib
import ipaddress
import logging
import pytest
import tempfile
import time

from scapy.all import sniff, IP
from scapy.contrib import bgp
from tests.common.helpers.bgp import BGPNeighbor
from tests.common.utilities import wait_until

from tests.common.helpers.assertions import pytest_assert
from tests.common.dualtor.mux_simulator_control import mux_server_url   # noqa F401
from tests.common.dualtor.mux_simulator_control import toggle_all_simulator_ports_to_rand_selected_tor_m    # noqa F401
from tests.common.helpers.constants import DEFAULT_NAMESPACE

pytestmark = [
    pytest.mark.topology("any"),
]

PEER_COUNT = 2
BGP_LOG_TMPL = "/tmp/bgp%d.pcap"
ANNOUNCED_SUBNETS = [
    "10.10.100.0/27",
    "10.10.100.32/27",
    "10.10.100.64/27",
    "10.10.100.96/27",
    "10.10.100.128/27"
]
NEIGHBOR_ASN0 = 61000
NEIGHBOR_ASN1 = 61001
NEIGHBOR_PORT0 = 11000
NEIGHBOR_PORT1 = 11001


@contextlib.contextmanager
def log_bgp_updates(duthost, iface, save_path, ns):
    """Capture bgp packets to file."""
    if iface == "any":
        # Scapy doesn't support LINUX_SLL2 (Linux cooked v2), and tcpdump on Bullseye
        # defaults to writing in that format when listening on any interface. Therefore,
        # have it use LINUX_SLL (Linux cooked) instead.
        start_pcap = "tcpdump -y LINUX_SLL -i %s -w %s port 179" % (iface, save_path)
    else:
        start_pcap = "tcpdump -i %s -w %s port 179" % (iface, save_path)
    # for multi-asic dut, add 'ip netns exec asicx' to the beggining of tcpdump cmd
    stop_pcap = "sudo pkill -f '%s%s'" % (duthost.asic_instance_from_namespace(ns).ns_arg, start_pcap)
    start_pcap = "nohup {}{} &".format(duthost.asic_instance_from_namespace(ns).ns_arg, start_pcap)
    duthost.shell(start_pcap)
    try:
        yield
    finally:
        duthost.shell(stop_pcap, module_ignore_errors=True)


@pytest.fixture
def is_quagga(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Return True if current bgp is using Quagga."""
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    show_res = duthost.asic_instance().run_vtysh("-c 'show version'")
    return "Quagga" in show_res["stdout"]


@pytest.fixture
def is_dualtor(tbinfo):
    return "dualtor" in tbinfo["topo"]["name"]


@pytest.fixture
def common_setup_teardown(duthosts, enum_rand_one_per_hwsku_frontend_hostname,
                          is_dualtor, is_quagga, ptfhost, setup_interfaces, tbinfo):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    conn0, conn1 = setup_interfaces
    conn0_ns = DEFAULT_NAMESPACE if "namespace" not in conn0.keys() else conn0["namespace"]
    conn1_ns = DEFAULT_NAMESPACE if "namespace" not in conn1.keys() else conn1["namespace"]
    pytest_assert(conn0_ns == conn1_ns, "Test fail for conn0 on {} and conn1 on {} \
                  started on different asics!".format(conn0_ns, conn1_ns))

    dut_asn = mg_facts["minigraph_bgp_asn"]

    dut_type = ''
    for k, v in mg_facts['minigraph_devices'].iteritems():
        if k == duthost.hostname:
            dut_type = v['type']

    if 'ToRRouter' in dut_type:
        neigh_type = 'LeafRouter'
    else:
        neigh_type = 'ToRRouter'

    bgp_neighbors = (
        BGPNeighbor(
            duthost,
            ptfhost,
            "pseudoswitch0",
            conn0["neighbor_addr"].split("/")[0],
            NEIGHBOR_ASN0,
            conn0["local_addr"].split("/")[0],
            dut_asn,
            NEIGHBOR_PORT0,
            neigh_type,
            conn0_ns,
            is_multihop=is_quagga or is_dualtor,
            is_passive=False
        ),
        BGPNeighbor(
            duthost,
            ptfhost,
            "pseudoswitch1",
            conn1["neighbor_addr"].split("/")[0],
            NEIGHBOR_ASN1,
            conn1["local_addr"].split("/")[0],
            dut_asn,
            NEIGHBOR_PORT1,
            neigh_type,
            conn1_ns,
            is_multihop=is_quagga or is_dualtor,
            is_passive=False
        )
    )

    return bgp_neighbors


@pytest.fixture
def constants(is_quagga, setup_interfaces):
    class _C(object):
        """Dummy class to save test constants."""
        pass

    _constants = _C()
    if is_quagga:
        _constants.sleep_interval = 40
        _constants.update_interval_threshold = 20
    else:
        _constants.sleep_interval = 5
        _constants.update_interval_threshold = 1

    conn0 = setup_interfaces[0]
    _constants.routes = []
    for subnet in ANNOUNCED_SUBNETS:
        _constants.routes.append(
            {"prefix": subnet, "nexthop": conn0["neighbor_addr"].split("/")[0]}
        )
    return _constants


def test_bgp_update_timer(common_setup_teardown, constants, duthosts, enum_rand_one_per_hwsku_frontend_hostname,
                          toggle_all_simulator_ports_to_rand_selected_tor_m):   # noqa F811

    def bgp_update_packets(pcap_file):
        """Get bgp update packets from pcap file."""
        packets = sniff(
            offline=pcap_file,
            lfilter=lambda p: IP in p and bgp.BGPHeader in p and p[bgp.BGPHeader].type == 2
        )
        return packets

    def match_bgp_update(packet, src_ip, dst_ip, action, route):
        """Check if the bgp update packet matches."""
        if not (packet[IP].src == src_ip and packet[IP].dst == dst_ip):
            return False
        subnet = ipaddress.ip_network(route["prefix"].decode())

        # New scapy (version 2.4.5) uses a different way to represent and dissect BGP messages. Below logic is to
        # address the compatibility issue of scapy versions.
        if hasattr(bgp, 'BGPNLRI_IPv4'):
            _route = bgp.BGPNLRI_IPv4(prefix=str(subnet))
        else:
            _route = (subnet.prefixlen, str(subnet.network_address))
        bgp_fields = packet[bgp.BGPUpdate].fields
        if action == "announce":
            # New scapy (version 2.4.5) uses a different way to represent and dissect BGP messages. Below logic is to
            # address the compatibility issue of scapy versions.
            path_attr_valid = False
            if "tp_len" in bgp_fields:
                path_attr_valid = bgp_fields['tp_len'] > 0
            elif "path_attr_len" in bgp_fields:
                path_attr_valid = bgp_fields["path_attr_len"] > 0
            return path_attr_valid and _route in bgp_fields["nlri"]
        elif action == "withdraw":
            # New scapy (version 2.4.5) uses a different way to represent and dissect BGP messages. Below logic is to
            # address the compatibility issue of scapy versions.
            withdrawn_len_valid = False
            if "withdrawn_len" in bgp_fields:
                withdrawn_len_valid = bgp_fields["withdrawn_len"] > 0
            elif "withdrawn_routes_len" in bgp_fields:
                withdrawn_len_valid = bgp_fields["withdrawn_routes_len"] > 0

            # New scapy (version 2.4.5) uses a different way to represent and dissect BGP messages. Below logic is to
            # address the compatibility issue of scapy versions.
            withdrawn_route_valid = False
            if "withdrawn" in bgp_fields:
                withdrawn_route_valid = _route in bgp_fields["withdrawn"]
            elif "withdrawn_routes" in bgp_fields:
                withdrawn_route_valid = _route in bgp_fields["withdrawn_routes"]

            return withdrawn_len_valid and withdrawn_route_valid
        else:
            return False

    def is_neighbor_sessions_established(duthost, neighbors):
        is_established = True

        # handle both multi-sic and single-asic
        bgp_facts = duthost.bgp_facts(num_npus=duthost.sonichost.num_asics())["ansible_facts"]
        for neighbor in neighbors:
            is_established &= neighbor.ip in bgp_facts["bgp_neighbors"] and bgp_facts["bgp_neighbors"][neighbor.ip]["state"] == "established"

        return is_established

    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    n0, n1 = common_setup_teardown
    try:
        n0.start_session()
        n1.start_session()

        # ensure new sessions are ready
        if not wait_until(90, 5, 20, lambda: is_neighbor_sessions_established(duthost, (n0, n1))):
            pytest.fail("Could not establish bgp sessions")

        announce_intervals = []
        withdraw_intervals = []
        for i, route in enumerate(constants.routes):
            bgp_pcap = BGP_LOG_TMPL % i
            with log_bgp_updates(duthost, "any", bgp_pcap, n0.namespace):
                n0.announce_route(route)
                time.sleep(constants.sleep_interval)
                n0.withdraw_route(route)
                time.sleep(constants.sleep_interval)

            with tempfile.NamedTemporaryFile() as tmp_pcap:
                duthost.fetch(src=bgp_pcap, dest=tmp_pcap.name, flat=True)
                duthost.file(path=bgp_pcap, state="absent")
                bgp_updates = bgp_update_packets(tmp_pcap.name)

            announce_from_n0_to_dut = []
            announce_from_dut_to_n1 = []
            withdraw_from_n0_to_dut = []
            withdraw_from_dut_to_n1 = []
            for bgp_update in bgp_updates:
                if match_bgp_update(bgp_update, n0.ip, n0.peer_ip, "announce", route):
                    announce_from_n0_to_dut.append(bgp_update)
                    continue
                if match_bgp_update(bgp_update, n1.peer_ip, n1.ip, "announce", route):
                    announce_from_dut_to_n1.append(bgp_update)
                    continue
                if match_bgp_update(bgp_update, n0.ip, n0.peer_ip, "withdraw", route):
                    withdraw_from_n0_to_dut.append(bgp_update)
                    continue
                if match_bgp_update(bgp_update, n1.peer_ip, n1.ip, "withdraw", route):
                    withdraw_from_dut_to_n1.append(bgp_update)

            err_msg = "no bgp update %s route %s from %s to %s"
            no_update = False
            if not announce_from_n0_to_dut:
                err_msg %= ("announce", route, n0.ip, n0.peer_ip)
                no_update = True
            elif not announce_from_dut_to_n1:
                err_msg %= ("announce", route, n1.peer_ip, n1.ip)
                no_update = True
            elif not withdraw_from_n0_to_dut:
                err_msg %= ("withdraw", route, n0.ip, n0.peer_ip)
                no_update = True
            elif not withdraw_from_dut_to_n1:
                err_msg %= ("withdraw", route, n1.peer_ip, n1.ip)
                no_update = True
            if no_update:
                pytest.fail(err_msg)

            announce_intervals.append(
                announce_from_dut_to_n1[0].time - announce_from_n0_to_dut[0].time
            )
            withdraw_intervals.append(
                withdraw_from_dut_to_n1[0].time - withdraw_from_n0_to_dut[0].time
            )

        logging.debug("announce updates intervals: %s", announce_intervals)
        logging.debug("withdraw updates intervals: %s", withdraw_intervals)

        mi = (len(constants.routes) - 1) // 2
        announce_intervals.sort()
        withdraw_intervals.sort()
        err_msg = "%s updates interval exceeds threshold %d"
        if announce_intervals[mi] >= constants.update_interval_threshold:
            pytest.fail(err_msg % ("announce", constants.update_interval_threshold))
        if withdraw_intervals[mi] >= constants.update_interval_threshold:
            pytest.fail(err_msg % ("withdraw", constants.update_interval_threshold))

    finally:
        n0.stop_session()
        n1.stop_session()
        for route in constants.routes:
            duthost.shell("ip route flush %s" % route["prefix"])
