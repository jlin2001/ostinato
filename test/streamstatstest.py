#! /usr/bin/env python

# standard modules
import ipaddress
import logging
import os
import pytest
import subprocess
import sys
import time

import pytest
from fabric.api import run, env, sudo

from utils import get_tshark

sys.path.insert(1, '../binding')
from core import ost_pb, emul, DroneProxy
from rpc import RpcError
from protocols.mac_pb2 import mac, Mac
from protocols.ip4_pb2 import ip4, Ip4
from protocols.payload_pb2 import payload, Payload
from protocols.sign_pb2 import sign

# Convenience class to interwork with OstEmul::Ip6Address() and
# the python ipaddress module
# FIXME: move to a common module for reuse and remove duplication in other
#        scripts
class ip6_address(ipaddress.IPv6Interface):
    def __init__(self, addr):
        if type(addr) is str:
            super(ip6_address, self).__init__(unicode(addr))
        elif type(addr) is int:
            super(ip6_address, self).__init__(addr)
        else:
            super(ip6_address, self).__init__(addr.hi << 64 | addr.lo)
        self.ip6 = emul.Ip6Address()
        self.ip6.hi = int(self) >> 64
        self.ip6.lo = int(self) & 0xffffffffffffffff

        self.prefixlen = self.network.prefixlen

        # we assume gateway is the lowest IP host address in the network
        gateway = self.network.network_address + 1
        self.gateway = emul.Ip6Address()
        self.gateway.hi = int(gateway) >> 64
        self.gateway.lo = int(gateway) & 0xffffffffffffffff

use_defaults = True

# initialize defaults
host_name = '127.0.0.1'

# initialize defaults - DUT
env.use_shell = False
env.user = 'tc'
env.password = 'tc'
env.host_string = 'localhost:50022'

tshark = get_tshark(minversion = '1.2.0') # FIXME: do we need a minversion?

# setup protocol number dictionary
# FIXME: remove if not reqd.
proto_number = {}
proto_number['mac'] = ost_pb.Protocol.kMacFieldNumber
proto_number['vlan'] = ost_pb.Protocol.kVlanFieldNumber
proto_number['eth2'] = ost_pb.Protocol.kEth2FieldNumber
proto_number['ip4'] = ost_pb.Protocol.kIp4FieldNumber
proto_number['ip6'] = ost_pb.Protocol.kIp6FieldNumber
proto_number['udp'] = ost_pb.Protocol.kUdpFieldNumber
proto_number['payload'] = ost_pb.Protocol.kPayloadFieldNumber

# setup logging
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# command-line option/arg processing
if len(sys.argv) > 1:
    if sys.argv[1] in ('-d', '--use-defaults'):
        use_defaults = True
    if sys.argv[1] in ('-h', '--help'):
        print('%s [OPTION]...' % (sys.argv[0]))
        print('Options:')
        print(' -d --use-defaults   run using default values')
        print(' -h --help           show this help')
        sys.exit(0)

print(' +-------+           +-------+')
print(' |       |X--<-->--X|-+     |')
print(' | Drone |          | | DUT |')
print(' |       |Y--<-->--Y|-+     |')
print(' +-------+           +-------+')
print('')
print('Drone has 2 ports connected to DUT. Packets sent on port X')
print('are expected to be forwarded by the DUT and received back on')
print('port Y and vice versa')
print('')

if not use_defaults:
    s = raw_input('Drone\'s Hostname/IP [%s]: ' % (host_name))
    host_name = s or host_name
    s = raw_input('DUT\'s Hostname/IP [%s]: ' % (env.host_string))
    env.host_string = s or env.host_string
    # FIXME: get inputs for dut x/y ports

@pytest.fixture(scope='module')
def drone(request):
    """Baseline Configuration for all testcases in this module"""

    drn = DroneProxy(host_name)

    log.info('connecting to drone(%s:%d)' % (drn.hostName(), drn.portNumber()))
    drn.connect()

    def fin():
        drn.disconnect()

    request.addfinalizer(fin)

    return drn

@pytest.fixture(scope='module')
def ports(request, drone):
    port_id_list = drone.getPortIdList()
    port_config_list = drone.getPortConfig(port_id_list)
    assert len(port_config_list.port) != 0

    # print port list and find default X/Y ports
    port_x_num = -1
    port_y_num = -1
    print port_config_list
    print('Port List')
    print('---------')
    for port in port_config_list.port:
        print('%d.%s (%s)' % (port.port_id.id, port.name, port.description))
        # use a vhost port as default X/Y port
        if ('vhost' in port.name or 'sun' in port.description.lower()):
            if port_x_num < 0:
                port_x_num = port.port_id.id
            elif port_y_num < 0:
                port_y_num = port.port_id.id
        if ('eth1' in port.name):
            port_x_num = port.port_id.id
        if ('eth2' in port.name):
            port_y_num = port.port_id.id

    assert port_x_num >= 0
    assert port_y_num >= 0

    print('Using port %d as port X' % port_x_num)
    print('Using port %d as port Y' % port_y_num)

    ports.x = ost_pb.PortIdList()
    ports.x.port_id.add().id = port_x_num;

    ports.y = ost_pb.PortIdList()
    ports.y.port_id.add().id = port_y_num;

    # Enable stream stats on ports
    portConfig = ost_pb.PortConfigList()
    portConfig.port.add().port_id.id = port_x_num;
    portConfig.port[0].stream_stats_tracking = True;
    portConfig.port.add().port_id.id = port_y_num;
    portConfig.port[1].stream_stats_tracking = True;
    print('Enabling Stream Stats tracking on ports X and Y');
    drone.modifyPort(portConfig);

    return ports

@pytest.fixture(scope='module')
def emul_ports(request, drone, ports):
    emul_ports = ost_pb.PortIdList()
    emul_ports.port_id.add().id = ports.x.port_id[0].id;
    emul_ports.port_id.add().id = ports.y.port_id[0].id;
    return emul_ports

@pytest.fixture(scope='module')
def dgid_list(request, drone, ports):
    # ----------------------------------------------------------------- #
    # create emulated device(s) on tx/rx ports - each test case will
    # use these same devices
    # ----------------------------------------------------------------- #

    # delete existing devices, if any, on tx port
    dgid_list.x = drone.getDeviceGroupIdList(ports.x.port_id[0])
    drone.deleteDeviceGroup(dgid_list.x)

    # add a emulated device group on port X
    dgid_list.x = ost_pb.DeviceGroupIdList()
    dgid_list.x.port_id.CopyFrom(ports.x.port_id[0])
    dgid_list.x.device_group_id.add().id = 1
    log.info('adding X device_group %d' % dgid_list.x.device_group_id[0].id)
    drone.addDeviceGroup(dgid_list.x)

    # configure the X device(s)
    devgrp_cfg = ost_pb.DeviceGroupConfigList()
    devgrp_cfg.port_id.CopyFrom(ports.x.port_id[0])
    dg = devgrp_cfg.device_group.add()
    dg.device_group_id.id = dgid_list.x.device_group_id[0].id
    dg.core.name = "HostX"

    dg.Extensions[emul.mac].address = 0x000102030a01

    dg.Extensions[emul.ip4].address = 0x0a0a0165
    dg.Extensions[emul.ip4].prefix_length = 24
    dg.Extensions[emul.ip4].default_gateway = 0x0a0a0101

    ip6addr = ip6_address('1234:1::65/96')
    dg.Extensions[emul.ip6].address.CopyFrom(ip6addr.ip6)
    dg.Extensions[emul.ip6].prefix_length = ip6addr.prefixlen
    dg.Extensions[emul.ip6].default_gateway.CopyFrom(ip6addr.gateway)

    drone.modifyDeviceGroup(devgrp_cfg)

    # delete existing devices, if any, on Y port
    dgid_list.y = drone.getDeviceGroupIdList(ports.y.port_id[0])
    drone.deleteDeviceGroup(dgid_list.y)

    # add a emulated device group on port Y
    dgid_list.y = ost_pb.DeviceGroupIdList()
    dgid_list.y.port_id.CopyFrom(ports.y.port_id[0])
    dgid_list.y.device_group_id.add().id = 1
    log.info('adding Y device_group %d' % dgid_list.y.device_group_id[0].id)
    drone.addDeviceGroup(dgid_list.y)

    # configure the Y device(s)
    devgrp_cfg = ost_pb.DeviceGroupConfigList()
    devgrp_cfg.port_id.CopyFrom(ports.y.port_id[0])
    dg = devgrp_cfg.device_group.add()
    dg.device_group_id.id = dgid_list.y.device_group_id[0].id
    dg.core.name = "HostY"

    dg.Extensions[emul.mac].address = 0x000102030b01

    dg.Extensions[emul.ip4].address = 0x0a0a0265
    dg.Extensions[emul.ip4].prefix_length = 24
    dg.Extensions[emul.ip4].default_gateway = 0x0a0a0201

    ip6addr = ip6_address('1234:2::65/96')
    dg.Extensions[emul.ip6].address.CopyFrom(ip6addr.ip6)
    dg.Extensions[emul.ip6].prefix_length = ip6addr.prefixlen
    dg.Extensions[emul.ip6].default_gateway.CopyFrom(ip6addr.gateway)

    drone.modifyDeviceGroup(devgrp_cfg)

    def fin():
        dgid_list = drone.getDeviceGroupIdList(ports.x.port_id[0])
        drone.deleteDeviceGroup(dgid_list)
        dgid_list = drone.getDeviceGroupIdList(ports.y.port_id[0])
        drone.deleteDeviceGroup(dgid_list)
    request.addfinalizer(fin)

    return dgid_list

@pytest.fixture(scope='module')
def dut(request):
    # Enable IP forwarding on the DUT (aka make it a router)
    sudo('sysctl -w net.ipv4.ip_forward=1')
    sudo('sysctl -w net.ipv6.conf.all.forwarding=1')

@pytest.fixture(scope='module')
def dut_ports(request):
    dut_ports.x = 'eth1'
    dut_ports.y = 'eth2'

    # delete all configuration on the DUT interfaces
    sudo('ip address flush dev ' + dut_ports.y)
    sudo('ip address flush dev ' + dut_ports.x)
    return dut_ports

@pytest.fixture
def dut_ip(request, dut_ports):
    sudo('ip address add 10.10.1.1/24 dev ' + dut_ports.x)
    sudo('ip address add 10.10.2.1/24 dev ' + dut_ports.y)

    sudo('ip -6 address add 1234:1::1/96 dev ' + dut_ports.x)
    sudo('ip -6 address add 1234:2::1/96 dev ' + dut_ports.y)

    def fin():
        sudo('ip address delete 10.10.1.1/24 dev ' + dut_ports.x)
        sudo('ip address delete 10.10.2.1/24 dev ' + dut_ports.y)

        sudo('ip -6 address delete 1234:1::1/96 dev ' + dut_ports.x)
        sudo('ip -6 address delete 1234:2::1/96 dev ' + dut_ports.y)
    request.addfinalizer(fin)

@pytest.fixture(scope='module')
def stream_clear(request, drone, ports):
    # delete existing streams, if any, on all ports
    sid_list = drone.getStreamIdList(ports.x.port_id[0])
    drone.deleteStream(sid_list)

@pytest.fixture(scope='module')
def stream(request, drone, ports):

    # add stream(s)
    stream_id = ost_pb.StreamIdList()
    stream_id.port_id.CopyFrom(ports.x.port_id[0])
    stream_id.stream_id.add().id = 1    # Unsigned stream
    stream_id.stream_id.add().id = 101  # Signed stream
    log.info('adding X stream(s) %d %d' %
               (stream_id.stream_id[0].id, stream_id.stream_id[1].id))
    drone.addStream(stream_id)

    # configure the stream(s)
    stream_cfg = ost_pb.StreamConfigList()
    stream_cfg.port_id.CopyFrom(ports.x.port_id[0])
    s = stream_cfg.stream.add()
    s.stream_id.id = stream_id.stream_id[0].id
    s.core.is_enabled = True
    s.control.packets_per_sec = 100
    s.control.num_packets = 10

    # setup (unsigned) stream protocols as mac:eth2:ip:payload
    p = s.protocol.add()
    p.protocol_id.id = ost_pb.Protocol.kMacFieldNumber
    p.Extensions[mac].dst_mac_mode = Mac.e_mm_resolve
    p.Extensions[mac].src_mac_mode = Mac.e_mm_resolve

    s.protocol.add().protocol_id.id = ost_pb.Protocol.kEth2FieldNumber

    p = s.protocol.add()
    p.protocol_id.id = ost_pb.Protocol.kIp4FieldNumber
    p.Extensions[ip4].src_ip = 0x0a0a0165
    p.Extensions[ip4].dst_ip = 0x0a0a0265

    s.protocol.add().protocol_id.id = ost_pb.Protocol.kPayloadFieldNumber

    # setup (signed) stream protocols as mac:eth2:ip:udp:payload
    # Remove payload, add udp, payload and sign protocol to signed stream(s)
    s = stream_cfg.stream.add()
    s.CopyFrom(stream_cfg.stream[0])
    s.stream_id.id = stream_id.stream_id[1].id
    del s.protocol[-1]
    s.protocol.add().protocol_id.id = ost_pb.Protocol.kUdpFieldNumber
    s.protocol.add().protocol_id.id = ost_pb.Protocol.kPayloadFieldNumber
    p = s.protocol.add()
    p.protocol_id.id = ost_pb.Protocol.kSignFieldNumber
    p.Extensions[sign].stream_guid = 101

    def fin():
        # delete streams
        log.info('deleting tx_stream %d' % stream_id.stream_id[0].id)
        drone.deleteStream(stream_id)

    request.addfinalizer(fin)

    return stream_cfg

@pytest.fixture(scope='module')
def stream_guids(request, drone, ports):
    stream_guids = ost_pb.StreamGuidList()
    stream_guids.stream_guid.add().id = 101
    stream_guids.port_list.port_id.add().id = ports.x.port_id[0].id;
    stream_guids.port_list.port_id.add().id = ports.y.port_id[0].id;
    return stream_guids

"""
# FIXME: remove if not required
protolist=['mac eth2 ip4 udp payload', 'mac eth2 ip4 udp']
@pytest.fixture(scope='module', params=protolist)
def stream_toggle_payload(request, drone, ports):
    global proto_number

    # add a stream
    stream_id = ost_pb.StreamIdList()
    stream_id.port_id.CopyFrom(ports.x.port_id[0])
    stream_id.stream_id.add().id = 1
    log.info('adding tx_stream %d' % stream_id.stream_id[0].id)
    drone.addStream(stream_id)

    # configure the stream
    stream_cfg = ost_pb.StreamConfigList()
    stream_cfg.port_id.CopyFrom(ports.x.port_id[0])
    s = stream_cfg.stream.add()
    s.stream_id.id = stream_id.stream_id[0].id
    s.core.is_enabled = True
    s.control.packets_per_sec = 100
    s.control.num_packets = 10

    # setup stream protocols
    s.ClearField("protocol")
    protos = request.param.split()
    for p in protos:
        s.protocol.add().protocol_id.id = proto_number[p]

    def fin():
        # delete streams
        log.info('deleting tx_stream %d' % stream_id.stream_id[0].id)
        drone.deleteStream(stream_id)

    request.addfinalizer(fin)

    return stream_cfg
"""

# ================================================================= #
# ----------------------------------------------------------------- #
#                            TEST CASES
# ----------------------------------------------------------------- #
# ================================================================= #

def test_unidir(drone, ports, dut, dut_ports, dut_ip, emul_ports, dgid_list,
        stream_clear, stream, stream_guids):
    """ TESTCASE: Verify that uni-directional stream stats are correct for a
        single signed stream X --> Y
                     DUT
                   /.1   \.1
                  /       \
            10.10.1/24  10.10.2/24
            1234::1/96  1234::2/96
                /           \
               /.101         \.101
            HostX           HostY
     """
    log.info('configuring stream %d on port X' % stream.stream[0].stream_id.id)
    drone.modifyStream(stream)

    # clear port X/Y stats
    log.info('clearing stats')
    drone.clearStats(ports.x)
    drone.clearStats(ports.y)
    drone.clearStreamStats(ports.x)
    drone.clearStreamStats(ports.y)

    # resolve ARP/NDP on ports X/Y
    log.info('resolving Neighbors on (X, Y) ports ...')
    drone.resolveDeviceNeighbors(emul_ports)
    time.sleep(3)

    # FIXME: dump ARP/NDP table on devices and DUT

    try:
        drone.startCapture(ports.y)
        drone.startTransmit(ports.x)
        log.info('waiting for transmit to finish ...')
        time.sleep(3)
        drone.stopTransmit(ports.x)
        drone.stopCapture(ports.y)

        # verify port stats
        x_stats = drone.getStats(ports.x)
        log.info('--> (x_stats)' + x_stats.__str__())
        assert(x_stats.port_stats[0].tx_pkts >= 20)

        y_stats = drone.getStats(ports.y)
        log.info('--> (y_stats)' + y_stats.__str__())
        assert(y_stats.port_stats[0].rx_pkts >= 20)

        # dump Y capture buffer
        log.info('getting Y capture buffer')
        buff = drone.getCaptureBuffer(ports.y.port_id[0])
        drone.saveCaptureBuffer(buff, 'capture.pcap')
        log.info('dumping Y capture buffer')
        cap_pkts = subprocess.check_output([tshark, '-n', '-r', 'capture.pcap'])
        print(cap_pkts)
        filter="frame[-9:9]==00.00.00.65.61.a1.b2.c3.d4"
        print(filter)
        log.info('dumping Y capture buffer (filtered)')
        cap_pkts = subprocess.check_output([tshark, '-n', '-r', 'capture.pcap',
            '-Y', filter])
        print(cap_pkts)
        assert cap_pkts.count('\n') == 10
        os.remove('capture.pcap')

        # verify stream stats
        stream_stats_list = drone.getStreamStats(stream_guids)
        log.info('--> (stream_stats)' + stream_stats_list.__str__())
        assert (len(stream_stats_list.stream_stats) > 0)

        # FIXME: verify stream stats

    except RpcError as e:
            raise
    finally:
        drone.stopTransmit(ports.x)

#
# TODO
#  * Verify that uni-directional stream stats are correct for a single stream
#  * Verify that uni-directional stream stats are correct for multiple streams
#  * Verify that bi-directional stream stats are correct for a single stream
#  * Verify that bi-directional stream stats are correct for multiple streams
#  * Verify protocol combinations - Eth, IPv4/IPv6, TCP/UDP, Pattern
#  * Verify transmit modes
#
