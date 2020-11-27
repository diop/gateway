""" sms_gateway.py - A example of using the goTenna API for a simple command line messaging application.

Usage: python sms_gateway.py
"""
from __future__ import print_function
import cmd # for the command line application
import sys # To quit
import os
import traceback
import logging
import math
import goTenna # The goTenna API
import re
import serial
import time
from time import sleep
import cbor
from txtenna import TxTenna
import threading
import socket
import select

BYTE_STRING_CBOR_TAG = 24
PHONE_NUMBER_CBOR_TAG = 25
MESSAGE_TEXT_CBOR_TAG = 26
SEGMENT_NUMBER_CBOR_TAG = 28
SEGMENT_COUNT_CBOR_TAG = 29
TXTENNA_ID_CBOR_TAG = 30
TRANSACTION_HASH_CBOR_TAG = 31
TXID_CBOR_TAG = 31
HOST_CBOR_TAG = 32
PORT_CBOR_TAG = 33
SOCKET_ID_CBOR_TAG = 34

# For SPI connection only, set SPI_CONNECTION to true with proper SPI settings
SPI_CONNECTION = False
SPI_BUS_NO = 0
SPI_CHIP_NO = 0
SPI_REQUEST = 22
SPI_READY = 27

# For socket connections
DEFAULT_BUF_SIZE = 6000
MESH_PAYLOAD_SIZE = 150

# Configure the Python logging module to print to stderr. In your application,
# you may want to route the logging elsewhere.
logging.basicConfig()

# Import readline if the system has it
try:
    import readline
    assert readline # silence pyflakes
except ImportError:
    pass

GATEWAY_GID = "555555555"
in_flight_events = {}

def mesh_socket_write(data, api_thread, gid, socket_id, in_flight_events):
    if api_thread is None or api_thread.connect is False:
        return None

    # wait until last message delivered or failed
    while (len(in_flight_events) > 0):
        sleep(10)

    try:
        method_callback = build_callback(in_flight_events)
        payload = goTenna.payload.BinaryPayload(data)
        payload.set_sender_initials('f')
        def ack_callback(correlation_id, success):
            if success:
                print("Private (socket) correlation_id={} message: delivery confirmed".format(correlation_id))
            else:
                print("Private (socket) correlation_id={} message: delivery not confirmed, recipient may be offline or out of range".format(correlation_id))

        gidobj = goTenna.settings.GID(int(gid), goTenna.settings.GID.PRIVATE)

        corr_id = None
        attempts = 0
        while corr_id is None and attempts < 5:
            corr_id = api_thread.send_private(gidobj, payload,
                                                    method_callback,
                                                    ack_callback=ack_callback,
                                                    encrypt=False)
            if corr_id is None:
                attempts += 1
                sleep(20)
    except ValueError:
        print("Message too long!")
        return

    if corr_id is not None:
        in_flight_events[corr_id.bytes]\
            = 'Private message from socket_id:{} sent to {}: corr_id={}'.format(socket_id.hex(), gid, corr_id)
    else:
        print("corr_id is None!")

    return corr_id

def build_callback(in_flight_events, error_handler=None):
    """ Build a callback for sending to the API thread. May speciy a callable
    error_handler(details) taking the error details from the callback. The handler should return a string.
    """
    def default_error_handler(details):
        """ Easy error handler if no special behavior is needed. Just builds a string with the error.
        """
        if details['code'] in [goTenna.constants.ErrorCodes.TIMEOUT,
                                goTenna.constants.ErrorCodes.OSERROR,
                                goTenna.constants.ErrorCodes.EXCEPTION]:
            return "USB connection disrupted"
        return "Error: {}: {}".format(details['code'], details['msg'])

    # Define a second function here so it implicitly captures self
    captured_error_handler = [error_handler]
    def callback(correlation_id, success=None, results=None,
                    error=None, details=None):
        """ The default callback to pass to the API.

        See the documentation for ``goTenna.driver``.

        Does nothing but print whether the method succeeded or failed.
        """
        method = in_flight_events.pop(correlation_id.bytes, 'Method call')
        if success:
            if results:
                print("{} succeeded: {}".format(method, results))
            else:
                print("{} succeeded!".format(method))
        elif error:
            if not captured_error_handler[0]:
                captured_error_handler[0] = default_error_handler
            print("{} failed: {}".format(method, captured_error_handler[0](details)))
    return callback

class MeshSocket:
    """demonstration class only
      - coded for clarity, not efficiency
    """

    def __init__(self, api_thread, gid, socket_id, in_flight_events):
        self.api_thread = api_thread
        self.gid = gid
        self.socket_id = socket_id
        self.socket_thread = None
        self.mesh_thread = None
        self.in_flight_events = in_flight_events
        self.buffer = b''
        self.write_to_mesh_queue=[]
        self.running = False

    def connect(self, host, port):
        self.sock = socket.socket()
        self.sock.settimeout(60000)
        self.sock = socket.create_connection((host, port))
        self.sock.setblocking(0)

    def disconnect(self):
        try:
            self.sock.close()
        except:
            print("Closing socket {}: exception".format(self.socket_id))

    def socket_send(self, data, index, count):
        print("Received (socket) {} of {} messages from {}: socket {}, length = {}".
            format(index, count, self.gid, self.socket_id, len(data)))
        self.buffer = self.buffer + data
        # if index == count - 1:
        totalsent = 0
        while totalsent < len(self.buffer):
            sent = self.sock.send(self.buffer[totalsent:])
            if sent == 0:
                raise RuntimeError("socket connection broken")
            totalsent = totalsent + sent
        self.buffer = b''

    def readytoread(self):
        ready_to_read, ready_to_write, in_error = \
               select.select([self.sock],[],[])
        return self.sock in ready_to_read 
    
    def run(self):
        self.socket_thread = threading.Thread(target=self.read_socket_thread, args=())
        self.mesh_thread = threading.Thread(target=self.write_mesh_thread, args=())
        self.running = True
        self.socket_thread.start()
        self.mesh_thread.start()

    def return_connect_failed(self):
        print("return_connect_failed, socket_id={}".format(self.socket_id))
        mesh_socket_write(b'', self.api_thread, self.gid, self.socket_id, self.in_flight_events)

    def read_socket_thread(self):
        timeout = 0
        retries = 0
        while self.running:
            try:
                data = self.sock.recv(DEFAULT_BUF_SIZE)
            except BlockingIOError:
                sleep(1)
                continue
            except ConnectionResetError:
                # TODO: stop socket threads that have been idle and/or when peers disconnect
                print("MeshSocket: read_socket_thread(), peer socket threw ConnectionResetError. {}/{}".format(str(retries), str(10)))
                if retries > 10:
                    self.running = False
                    break
                retries = retries + 1
                continue
            if data != b'':
                count = int(math.ceil(len(data) / MESH_PAYLOAD_SIZE))
                for index in range(0, count) :
                    offset = index * MESH_PAYLOAD_SIZE
                    end = min([offset+MESH_PAYLOAD_SIZE, len(data)])
                    d = {
                        SOCKET_ID_CBOR_TAG : self.socket_id,
                        SEGMENT_NUMBER_CBOR_TAG : index,
                        SEGMENT_COUNT_CBOR_TAG : count,
                        BYTE_STRING_CBOR_TAG : data[offset:end]
                    }
                    protocol_msg = cbor.dumps(d)
                    print("Queue socket_id: {} from {}, {}/{}, len: {}, data: [{}]".format(self.socket_id.hex(), self.sock.getpeername(), index, count, len(protocol_msg), str(protocol_msg[-8:])))
                    self.write_to_mesh_queue.append(protocol_msg)
                timeout = 0
            else:
                sleep(1)
                timeout += 1
            if (timeout > 600):
                # stop socket thread that is idle for > 10 min
                self.running = False

    def write_mesh_thread(self):
        while self.running:
            if (len(self.write_to_mesh_queue) > 0):
                data = self.write_to_mesh_queue.pop(0)
                print("Pop socket_id: {}, len: {}, data: [{}]".format(self.socket_id.hex(), len(data), str(data[-8:])))
                # blocks if data rate is exceeded 
                mesh_socket_write(data, self.api_thread, self.gid, self.socket_id, self.in_flight_events)
            else:
                sleep(1)
        self.disconnect()

class goTennaCLI(cmd.Cmd):
    """ CLI handler function
    """
    def __init__(self):
        self.api_thread = None
        self.status = {}
        cmd.Cmd.__init__(self)
        self.prompt = 'SMS Gateway>'
        self.in_flight_events = {}
        self._set_frequencies = False
        self._set_tx_power = False
        self._set_bandwidth = False
        self._set_geo_region = False
        self._settings = goTenna.settings.GoTennaSettings(
            rf_settings=goTenna.settings.RFSettings(), 
            geo_settings=goTenna.settings.GeoSettings())
        self._do_encryption = False
        self._awaiting_disconnect_after_fw_update = [False]
        self.serial_port = None
        self.serial_rate = 115200
        self.sms_sender_dict = {}
        self.txtenna = None
        self.serial = None

        self.socket_dict = {}

        # prevent threads from accessing serial port simultaneiously
        self.serial_lock = threading.Lock() 

    def precmd(self, line):
        if not self.api_thread\
           and not line.startswith('sdk_token')\
           and not line.startswith('quit'):
            print("An SDK token must be entered to begin.")
            return ''
        return line

    def do_sdk_token(self, rst):
        """ Enter an SDK token to begin usage of the driver. Usage: sdk_token TOKEN"""
        if self.api_thread:
            print("To change SDK tokens, restart the sample app.")
            return
        try:
            if not SPI_CONNECTION:
                self.api_thread = goTenna.driver.Driver(sdk_token=rst, gid=None, 
                                                    settings=None, 
                                                    event_callback=self.event_callback)
            else:
                self.api_thread = goTenna.driver.SpiDriver(
                                    SPI_BUS_NO, SPI_CHIP_NO, 22, 27,
                                    rst, None, None, self.event_callback)
            self.api_thread.start()
        except ValueError:
            print("SDK token {} is not valid. Please enter a valid SDK token."
                  .format(rst))

    def emptyline(self):
        pass

    def event_callback(self, evt):
        """ The event callback that will print messages from the API.

        See the documentation for ``goTenna.driver``.

        This will be invoked from the API's thread when events are received.
        """
        if evt.event_type == goTenna.driver.Event.MESSAGE:
            try:
                if type(evt.message.payload) == goTenna.payload.BinaryPayload:
                    protocol_msg = cbor.loads(evt.message.payload._binary_data)
                    if PHONE_NUMBER_CBOR_TAG in protocol_msg:
                        phone_number = str(protocol_msg[PHONE_NUMBER_CBOR_TAG])
                        text_message = protocol_msg[MESSAGE_TEXT_CBOR_TAG]
                        self.do_send_sms("+" + phone_number + " " + text_message)
                        self.sms_sender_dict[phone_number.encode()] = str(evt.message.sender.gid_val).encode()
                    elif TXTENNA_ID_CBOR_TAG in protocol_msg:
                        if self.txtenna != None:
                            self.txtenna.handle_cbor_message(evt.message.sender.gid_val, protocol_msg)
                        else:
                            print("TxTenna BinaryPayload received but ignored.")
                    elif HOST_CBOR_TAG in protocol_msg and PORT_CBOR_TAG in protocol_msg and SOCKET_ID_CBOR_TAG in protocol_msg:
                        host = protocol_msg[HOST_CBOR_TAG]
                        port = protocol_msg[PORT_CBOR_TAG]
                        socket_id = protocol_msg[SOCKET_ID_CBOR_TAG]
                        gid = str(evt.message.sender.gid_val).encode()
                        count = protocol_msg[SEGMENT_COUNT_CBOR_TAG]

                        if socket_id not in self.socket_dict or self.socket_dict[socket_id].running == False:
                            self.socket_dict[socket_id] = MeshSocket(self.api_thread, gid, socket_id, self.in_flight_events)
                            try:
                                self.socket_dict[socket_id].connect(host, port)
                            except ConnectionRefusedError:
                                print("Reply to mesh sender {} that socket failed to open!".format(gid))
                                self.socket_dict[socket_id].return_connect_failed()
                                return
                            except Exception: # pylint: disable=broad-except
                                traceback.print_exc()
                                print("Reply to mesh sender {} that socket failed to open!".format(gid))
                                self.socket_dict[socket_id].return_connect_failed()
                                return
                            self.socket_dict[socket_id].run()

                        if BYTE_STRING_CBOR_TAG in protocol_msg:
                            data = protocol_msg[BYTE_STRING_CBOR_TAG]
                            self.socket_dict[socket_id].socket_send(data, 0, count)

                    elif BYTE_STRING_CBOR_TAG in protocol_msg and SOCKET_ID_CBOR_TAG in protocol_msg:
                        socket_id = protocol_msg[SOCKET_ID_CBOR_TAG]
                        if socket_id in self.socket_dict:
                            data = protocol_msg[BYTE_STRING_CBOR_TAG]
                            count = protocol_msg[SEGMENT_COUNT_CBOR_TAG]
                            index = protocol_msg[SEGMENT_NUMBER_CBOR_TAG]
                            self.socket_dict[socket_id].socket_send(data, index, count)
                    else:
                        print("Unknown BinaryPayload.")
                elif type(evt.message.payload) == goTenna.payload.CustomPayload:
                    print("Unknown CustomPayload.")
                else:
                    # check for correct (legacy) text SMS format
                    parsed_payload = re.fullmatch(r"([\+]?)([0-9]{9,15})\s(.+)", evt.message.payload.message)
                    if parsed_payload != None:
                        phone_number = parsed_payload[2]
                        text_message = parsed_payload[3]
                        # send to SMS Modem
                        self.do_send_sms("+" + phone_number + " " + text_message)
                        
                        # keep track of mapping of sms destination to mesh sender
                        self.sms_sender_dict[phone_number.encode()] = str(evt.message.sender.gid_val).encode()
            except BrokenPipeError:
                self.socket_dict.pop(socket_id)
            except ConnectionResetError:
                self.socket_dict.pop(socket_id)
            except ConnectionRefusedError:
                self.socket_dict.pop(socket_id)
            except Exception: # pylint: disable=broad-except
                traceback.print_exc()
        elif evt.event_type == goTenna.driver.Event.DEVICE_PRESENT:
            print(str(evt))
            if self._awaiting_disconnect_after_fw_update[0]:
                print("Device physically connected")
            else:
                print("Device physically connected, configure to continue")
        elif evt.event_type == goTenna.driver.Event.CONNECT:
            if self._awaiting_disconnect_after_fw_update[0]:
                print("Device reconnected! Firmware update complete!")
                self._awaiting_disconnect_after_fw_update[0] = False
            else:
                print("Connected!")
                print(str(evt))
        elif evt.event_type == goTenna.driver.Event.DISCONNECT:
            if self._awaiting_disconnect_after_fw_update[0]:
                # Do not reset configuration so that the device will reconnect on its own
                print("Firmware update: Device disconnected, awaiting reconnect")
            else:
                print("Disconnected! {}".format(evt))
                # We reset the configuration here so that if the user plugs in a different
                # device it is not immediately reconfigured with new and incorrect data
                self.api_thread.set_gid(None)
                self.api_thread.set_rf_settings(None)
                self._set_frequencies = False
                self._set_tx_power = False
                self._set_bandwidth = False
        elif evt.event_type == goTenna.driver.Event.STATUS:
            self.status = evt.status
            if self.serial != None:
                # check for unread SMS messages
                # self.do_read_sms("", self.forward_to_mesh)
                return

        elif evt.event_type == goTenna.driver.Event.GROUP_CREATE:
            index = -1
            for idx, member in enumerate(evt.group.members):
                if member.gid_val == self.api_thread.gid.gid_val:
                    index = idx
                    break
            print("Added to group {}: You are member {}"
                  .format(evt.group.gid.gid_val,
                          index))

    def do_set_gid(self, rem):
        """ Create a new profile (if it does not already exist) with default settings.

        Usage: make_profile GID

        GID should be a 15-digit numerical GID.
        """
        if self.api_thread.connected:
            print("Must not be connected when setting GID")
            return
        (gid, _) = self._parse_gid(rem, goTenna.settings.GID.PRIVATE)
        if not gid:
            return
        self.api_thread.set_gid(gid)

    def do_create_group(self, rem):
        """ Create a new group and send invitations to other members.

        Usage create_group GIDs...

        GIDs should be a list of the private GIDs of the other members of the group. The group will be created and stored on the connected goTenna and invitations will be sent to the other members.
        """
        if not self.api_thread.connected:
            print("Must be connected when creating a group")
            return
        gids = [self.api_thread.gid]
        while True:
            (this_gid, rem) = self._parse_gid(rem,
                                              goTenna.settings.GID.PRIVATE,
                                              False)
            if not this_gid:
                break
            gids.append(this_gid)
        if len(gids) < 2:
            print("The group must have at least one other member.")
            return
        group = goTenna.settings.Group.create_new(gids)
        def _invite_callback(correlation_id, member_index,
                             success=None, error=None, details=None):
            if success:
                msg = 'succeeded'
                to_print = ''
            elif error:
                msg = 'failed'
                to_print = ': ' + str(details)
            print("Invitation of {} to {} {}{}".format(gids[member_index],
                                                       group.gid.gid_val,
                                                       msg, to_print))
        def method_callback(correlation_id, success=None, results=None,
                            error=None, details=None):
            """ Custom callback for group creation
            """
            # pylint: disable=unused-argument
            if success:
                print("Group {} created!".format(group.gid.gid_val))
            elif error:
                print("Group {} could not be created: {}: {}"
                      .format(group.gid.gid_val,
                              details['code'], details['msg']))
        print("Creating group {}".format(group.gid.gid_val))
        corr_id = self.api_thread.add_group(group,
                                            method_callback,
                                            True,
                                            _invite_callback)
        self.in_flight_events[corr_id.bytes] = 'Group creation of {}'\
            .format(group.gid.gid_val)

    def do_resend_invite(self, rem):
        """ Resend an invitation to a group to a specific member.

        Usage resend_invite GROUP_GID MEMBER_GID

        The GROUP_GID must be a previously-created group.
        The MEMBER_GID must be a previously-specified member of the group.
        """
        if not self.api_thread.connected:
            print("Must be connected when resending a group invite")
            return
        group_gid, rem = self._parse_gid(rem, goTenna.settings.GID.GROUP)
        member_gid, rem = self._parse_gid(rem, goTenna.settings.GID.PRIVATE)
        if not group_gid or not member_gid:
            print("Must specify group GID and member GID to invite")
            return
        group_to_invite = None
        for group in self.api_thread.groups:
            if group.gid.gid_val == group_gid.gid_val:
                group_to_invite = group
                break
        else:
            print("No group found matching GID {}".format(group_gid.gid_val))
            return
        member_idx = None
        for idx, member in enumerate(group_to_invite.members):
            if member.gid_val == member_gid.gid_val:
                member_idx = idx
                break
        else:
            print("Group {} has no member {}".format(group_gid.gid_val,
                                                     member_gid.gid_val))
            return
        def ack_callback(correlation_id, success):
            if success:
                print("Invitation of {} to {}: delivery confirmed"
                      .format(member_gid.gid_val, group_gid.gid_val))
            else:
                print("Invitation of {} to {}: delivery unconfirmed, recipient may be offline or out of range"
                      .format(member_gid.gid_val, group_gid.gid_val))
        corr_id = self.api_thread.invite_to_group(group_to_invite, member_idx,
                                                  build_callback(self.in_flight_events),
                                                  ack_callback=ack_callback)
        self.in_flight_events[corr_id.bytes] = 'Invitation of {} to {}'\
            .format(group_gid.gid_val, member_gid.gid_val)

    def do_remove_group(self, rem):
        """ Remove a group.

        Usage remove_group GROUP_GID

        GROUP_GID should be a group GID.
        """
        if not self.api_thread.connected:
            print("Must be connected when resending a group invite")
            return
        group_gid, rem = self._parse_gid(rem, goTenna.settings.GID.GROUP)

        if not group_gid:
            print("Must specify group GID to remove it")
            return

        group_to_remove = None
        for group in self.api_thread.groups:
            if group.gid.gid_val == group_gid.gid_val:
                group_to_remove = group
                break
        else:
            print("No group found matching GID {}".format(group_gid.gid_val))
            return

        def method_callback(correlation_id, success=None, results=None,
                            error=None, details=None):
            # logger.debug(" ")
            """ Custom callback for group removal
            """
            # pylint: disable=unused-argument
            if success:
                print("Group {} removed!".format(group_to_remove.gid.gid_val))
            elif error:
                print("Group {} could not be removed: {}: {}"
                      .format(group_gid.gid_val,
                              details['code'], details['msg']))

        corr_id = self.api_thread.remove_group(group, method_callback)
        self.in_flight_events[corr_id.bytes] = 'Group removing of {}' \
            .format(group_gid.gid_val)

    def preloop(self):
        """Initialization before prompting user for commands.
           Despite the claims in the Cmd documentaion, Cmd.preloop() is not a stub.
        """
        cmd.Cmd.preloop(self)   ## sets up command completion
        self._hist    = []      ## No history yet
        self._locals  = {}      ## Initialize execution namespace for user
        self._globals = {}
        
        """ skip GSM modem
        if self.serial_port != None:
            self.do_init_sms("")

        if self.serial != None:
            self.do_delete_sms("")
        """

    def do_quit(self, arg):
        """ Safely quit.

        Usage: quit
        """
        # pylint: disable=unused-argument
        if self.api_thread:
            self.api_thread.join()
        if self.serial and self.serial.is_open:
            self.serial.close()
            self.serial = None

        return True

    def do_echo(self, rem):
        """ Send an echo command

        Usage: echo
        """
        if not self.api_thread.connected:
            print("No device connected")
        else:
            def error_handler(details):
                """ A special error handler for formatting message failures
                """
                if details['code'] in [goTenna.constants.ErrorCodes.TIMEOUT,
                                       goTenna.constants.ErrorCodes.OSERROR]:
                    return "Echo command may not have been sent: USB connection disrupted"
                return "Error sending echo command: {}".format(details)

            try:
                method_callback = build_callback(self.in_flight_events, error_handler)
                corr_id = self.api_thread.echo(method_callback)
            except ValueError:
                print("Echo failed!")
                return
            self.in_flight_events[corr_id.bytes] = 'Echo Send'

    def do_send_broadcast(self, message):
        """ Send a broadcast message

        Usage: send_broadcast MESSAGE
        """
        if not self.api_thread.connected:
            print("No device connected")
        else:
            try:
                method_callback = build_callback(self.in_flight_events)
                payload = goTenna.payload.TextPayload(message)
                corr_id = self.api_thread.send_broadcast(payload,
                                                         method_callback)
            except ValueError:
                print("Message too long!")
                return
            self.in_flight_events[corr_id.bytes] = 'Broadcast message: {}'.format(message)

    @staticmethod
    def _parse_gid(line, gid_type, print_message=True):
        parts = line.split(' ')
        remainder = ' '.join(parts[1:])
        gidpart = parts[0]
        try:
            gid = int(gidpart)
            if gid > goTenna.constants.GID_MAX:
                print('{} is not a valid GID. The maximum GID is {}'
                      .format(str(gid), str(goTenna.constants.GID_MAX)))
                return (None, line)
            gidobj = goTenna.settings.GID(gid, gid_type)
            return (gidobj, remainder)
        except ValueError:
            if print_message:
                print('{} is not a valid GID.'.format(line))
            return (None, remainder)

    def do_send_private(self, rem):
        """ Send a private message to a contact

        Usage: send_private GID MESSAGE

        GID is the GID to send the private message to.

        MESSAGE is the message.
        """
        if not self.api_thread.connected:
            print("Must connect first")
            return
        (gid, rest) = self._parse_gid(rem, goTenna.settings.GID.PRIVATE)
        if not gid:
            return
        message = rest

        try:
            method_callback = build_callback(self.in_flight_events)
            payload = goTenna.payload.TextPayload(message)
            def ack_callback(correlation_id, success):
                if success:
                    print("Private message to {}: delivery confirmed"
                          .format(gid.gid_val))
                else:
                    print("Private message to {}: delivery not confirmed, recipient may be offline or out of range"
                          .format(gid.gid_val))
            corr_id = self.api_thread.send_private(gid, payload,
                                                   method_callback,
                                                   ack_callback=ack_callback,
                                                   encrypt=self._do_encryption)
        except ValueError:
            print("Message too long!")
            return
        self.in_flight_events[corr_id.bytes]\
            = 'Private message to {}: {}'.format(gid.gid_val, message)

    def do_send_group(self, rem):
        """ Send a message to a group.

        Usage: send_group GROUP_GID MESSAGE

        GROUP_GID is the GID of the group to send the message to. This must have been previously loaded into the API, whether by receiving an invitation, using add_group, or using create_group.
        """
        if not self.api_thread.connected:
            print("Must connect first.")
            return
        (gid, rest) = self._parse_gid(rem, goTenna.settings.GID.GROUP)
        if not gid:
            return
        message = rest
        group = None
        for group in self.api_thread.groups:
            if gid.gid_val == group.gid.gid_val:
                group = group
                break
        else:
            print("Group {} is not known".format(gid.gid_val))
            return
        try:
            payload = goTenna.payload.TextPayload(message)
            corr_id = self.api_thread.send_group(group, payload,
                                                 build_callback(self.in_flight_events),
                                                 encrypt=self._do_encryption)
        except ValueError:
            print("message too long!")
            return
        self.in_flight_events[corr_id.bytes] = 'Group message to {}: {}'\
            .format(group.gid.gid_val, message)

    def get_device_type(self):
        return self.api_thread.device_type

    def do_set_transmit_power(self, rem):
        """ Set the transmit power of the device.

        Usage: set_transmit_power POWER

        POWER should be a string, one of 'HALF_W', 'ONE_W', 'TWO_W' or 'FIVE_W'
        """
        if self.get_device_type() == "900":
             print("This configuration cannot be done for Mesh devices.")
             return
        ok_args = [attr
                   for attr in dir(goTenna.constants.POWERLEVELS)
                   if attr.endswith('W')]
        if rem.strip() not in ok_args:
            print("Invalid power setting {}".format(rem))
            return
        power = getattr(goTenna.constants.POWERLEVELS, rem.strip())
        self._set_tx_power = True
        self._settings.rf_settings.power_enum = power
        self._maybe_update_rf_settings()

    def do_list_bandwidth(self, rem):
        """ List the available bandwidth.

        Usage: list_bandwidth
        """
        print("Allowed bandwidth in kHz: {}"
                .format(str(goTenna.constants.BANDWIDTH_KHZ[0].allowed_bandwidth)))

    def do_set_bandwidth(self, rem):
        """ Set the bandwidth for the device.

        Usage: set_bandwidth BANDWIDTH

        BANDWIDTH should be a bandwidth in kHz.

        Allowed bandwidth can be displayed with list_bandwidth.
        """
        if self.get_device_type() == "900":
            print("This configuration cannot be done for Mesh devices.")
            return
        bw_val = float(rem.strip())
        for bw in goTenna.constants.BANDWIDTH_KHZ:
            if bw.bandwidth == bw_val:
                bandwidth = bw
                break
        else:
            print("{} is not a valid bandwidth".format(bw_val))
            return
        self._settings.rf_settings.bandwidth = bandwidth
        self._set_bandwidth = True
        self._maybe_update_rf_settings()

    def _maybe_update_rf_settings(self):
        if self._set_tx_power\
           and self._set_frequencies\
           and self._set_bandwidth:
            self.api_thread.set_rf_settings(self._settings.rf_settings)

    def do_set_frequencies(self, rem):
        """ Configure the frequencies the device will use.

        Usage: set_frequencies CONTROL_FREQ DATA_FREQS....

        All arguments should be frequencies in Hz. The first argument will be used as the control frequency. Subsequent arguments will be data frequencies.
        """
        freqs = rem.split(' ')
        if len(freqs) < 2:
            print("At least one control frequency and one data frequency are required")
            return
        def _check_bands(freq):
            bad = True
            for band in goTenna.constants.BANDS:
                if freq >= band[0] and freq <= band[1]:
                    bad = False
            return bad

        if self.get_device_type() == "900":
            print("This configuration cannot be done for Mesh devices.")
            return
        try:
            control_freq = int(freqs[0])
        except ValueError:
            print("Bad control freq {}".format(freqs[0]))
            return
        if _check_bands(control_freq):
            print("Control freq out of range")
            return
        data_freqs = []
        for idx, freq in enumerate(freqs[1:]):
            try:
                converted_freq = int(freq)
            except ValueError:
                print("Data frequency {}: {} is bad".format(idx, freq))
                return
            if _check_bands(converted_freq):
                print("Data frequency {}: {} is out of range".format(idx, freq))
                return
            data_freqs.append(converted_freq)
        self._settings.rf_settings.control_freqs = [control_freq]
        self._settings.rf_settings.data_freqs = data_freqs
        self._set_frequencies = True
        self._maybe_update_rf_settings()

    def do_list_geo_region(self, rem):
        """ List the available region.

        Usage: list_geo_region
        """
        print("Allowed region:")
        for region in goTenna.constants.GEO_REGION.DICT:
            print("region {} : {}"
                  .format(region, goTenna.constants.GEO_REGION.DICT[region]))

    def do_set_geo_region(self, rem):
        """ Configure the frequencies the device will use.

        Usage: set_geo_region REGION

        Allowed region displayed with list_geo_region.
        """
        if self.get_device_type() == "pro":
            print("This configuration cannot be done for Pro devices.")
            return
        region = int(rem.strip())
        print('region={}'.format(region))
        if not goTenna.constants.GEO_REGION.valid(region):
            print("Invalid region setting {}".format(rem))
            return
        self._set_geo_region = True
        self._settings.geo_settings.region = region
        self.api_thread.set_geo_settings(self._settings.geo_settings)

    def do_can_connect(self, rem):
        """ Return whether a goTenna can connect. For a goTenna to connect, a GID and RF settings must be configured.
        """
        # pylint: disable=unused-argument
        if self.api_thread.gid:
            print("GID: OK")
        else:
            print("GID: Not Set")
        if self._set_tx_power:
            print("PRO - TX Power: OK")
        else:
            print("PRO - TX Power: Not Set")
        if self._set_frequencies:
            print("PRO - Frequencies: OK")
        else:
            print("PRO - Frequencies: Not Set")
        if self._set_bandwidth:
            print("PRO - Bandwidth: OK")
        else:
            print("PRO - Bandwidth: Not Set")
        if self._set_geo_region:
            print("MESH - Geo region: OK")
        else:
            print("MESH - Geo region: Not Set")


    def do_list_groups(self, arg):
        """ List the known groups """
        if not self.api_thread:
            print("The SDK must be configured first.")
            return
        if not self.api_thread.groups:
            print("No known groups.")
            return
        for group in self.api_thread.groups:
            print("Group GID {}: Other members {}"
                  .format(group.gid.gid_val,
                          ', '.join([str(m.gid_val)
                                     for m in group.members
                                     if m != self.api_thread.gid])))

    @staticmethod
    def _version_from_path(path):
        name = os.path.basename(path)
        parts = name.split('.')
        if len(parts) < 3:
            return None
        return (int(parts[0]),
                int(parts[1]),
                int(parts[2]))

    @staticmethod
    def _parse_version(args):
        version_part = args.split('.')
        if len(version_part) < 3:
            return None, args
        return (int(version_part[0]),
                int(version_part[1]),
                int(version_part[2])),\
                '.'.join(version_part[3:])

    @staticmethod
    def _parse_file(args):
        remainder = ''
        if '"' in args:
            parts = args.split('"')
            firmware_file = parts[1]
            remainder = '"'.join(parts[2:])
        else:
            parts = args.split(' ')
            firmware_file = parts[0]
            remainder = ' '.join(parts[1:])
        if not os.path.exists(firmware_file):
            return None, args
        return firmware_file, remainder

    def do_firmware_update(self, args):
        """ Update the device firmware.

        Usage: firmware_update FIRMWARE_FILE [VERSION]
        FIRMWARE_FILE should be the path to a binary firmware. Files or paths containing spaces should be specified in quotes.
        VERSION is an optional dotted version string. The first three dotted segments will determine the version stored in the firmware. If this argument is not passed, the command will try to deduce the version from the filename. If this deduction fails, the command aborts.
        """
        if not self.api_thread.connected:
            print("Device must be connected.")
            return
        firmware_file, rem = self._parse_file(args)
        if not firmware_file:
            print("Cannot find file {}".format(args))
            return
        try:
            version = self._version_from_path(firmware_file)
        except ValueError:
            version = None
        if not version:
            try:
                version, _ = self._parse_version(rem)
            except ValueError:
                print("Version must be 3 numbers separated by '.'")
                return
            if not version:
                print("Version must be specified when not in the filename")
                return
        try:
            open(firmware_file, 'rb').close()
        except (IOError, OSError) as caught_exc:
            print("Cannot open file {}: {}: {}"
                  .format(firmware_file, caught_exc.errno, caught_exc.strerror))
            return
        else:
            print("File {} OK".format(firmware_file))
            print("Beginning firmware update")

        def _callback(correlation_id,
                      success=None, error=None, details=None, results=None):
            # pylint: disable=unused-argument
            if success:
                print("Firmware updated!")
            elif error:
                print("Error updating firmware: {}: {}"
                      .format(details.get('code', 'unknown'),
                              details.get('msg', 'unknown')))
            self.prompt = "goTenna>"

        last_progress = [0]
        print("")
        def _progress_callback(progress, **kwargs):
            percentage = int(progress*100)
            if percentage/10 != last_progress[0]/10:
                last_progress[0] = percentage
                print("FW Update Progress: {: 3}%.".format(percentage))
                if last_progress[0] >= 90:
                    self._awaiting_disconnect_after_fw_update[0] = True
                if last_progress[0] >= 100:
                    print("Device will disconnect and reconnect when update complete.")

        self.prompt = "(updating firmware)"
        self.api_thread.update_firmware(firmware_file, _callback,
                                        _progress_callback, version)

    def do_get_system_info(self, args):
        """ Get system information.

        Usage: get_system_info
        """
        if not self.api_thread.connected:
            print("Device must be connected")
        print(self.api_thread.system_info)

    def send_ser_command(self, command):
        self.serial.write(command)
        sleep(0.5)
        ret = b''
        n = self.serial.in_waiting
        while True:
            if n > 0:
                ret += self.serial.read(n)
            else:
                sleep(.5)
            n = self.serial.in_waiting
            if n == 0:
                break

        return ret

    def do_send_sms(self, args):
        """ Send an SMS message to a particular phone number.

        Usage: send_sms PHONE_NUMBER MESSAGE
        """
        SEND_SMS = b'AT+CMGS="%b"\r'
        SEND_CLOSE = b'\x1A\r'	#sending CTRL-Z

        try:

            # zero or one '+', 9-15 digit phone number,whitespace,message text of 1-n characters
            payload = re.fullmatch(r"([\+]?)([0-9]{9,15})\s(.+)", args)

            phone_number = '+' + payload[2]
            message = payload[3]

            print("Sending SMS to {}".format(phone_number))
            print ("Message: ", message)

            with self.serial_lock:
                # send SMS message
                self.send_ser_command(SEND_SMS % phone_number.encode())
                self.send_ser_command(message.encode()+SEND_CLOSE)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

    def print_messages(self, msgs):

        print("Received {} messages:".format(len(msgs)))
        for m in msgs:
            print("Phone Number: {}".format(m['phone_number']))
            print("Received: {}".format(m['received']))
            print("Message: {}".format(m['message']))

    def forward_to_mesh(self, msgs):

        print("Received {} messages:".format(len(msgs)))
        for m in msgs:
            print("\tReceived: {}".format(m['received']))
            print("\tMessage: {}".format(m['message']))
            
            if m['phone_number'] in self.sms_sender_dict:
                mesh_sender_gid = self.sms_sender_dict[m['phone_number']]
                print("\tForwarding message from {} to mesh GID {}:".format(m['phone_number'], mesh_sender_gid))
                args = str(mesh_sender_gid+b' '+m['phone_number']+b' '+m['message'], 'utf-8')
                self.do_send_private(args)
            else:
                print("\tBroadcasting message from {} to mesh:".format(m['phone_number']))
                args = str(m['phone_number']+b' '+m['message'], 'utf-8')
                self.do_send_broadcast(args)

    def do_read_sms(self, args, callback=None):
        """ Read all unread SMS messages received.

        Usage: read_sms
        """
        RETRIEVE_UNREAD = b'AT+CMGL="REC UNREAD"\r'
        msgs = []

        try:
            with self.serial_lock:
                # retrieve all unread SMS messages
                ret = self.send_ser_command(RETRIEVE_UNREAD)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

        lines = [line for line in ret.split(b'\r\n') if line.strip() != b'']

        if len(lines) == 0 or lines[0] != RETRIEVE_UNREAD:
            return

        if len(lines) >= 2:
            for n in range(1, len(lines), 2):
                if lines[n] != b'OK':
                    fields = lines[n].split(b",")
                    if len(fields) > 3:
                        phone_number = fields[2].strip(b'"')
                        received = fields[4].strip(b'"')
                        message = lines[n+1]
                        msgs.append({'phone_number':phone_number, 'received':received, 'message':message})

        if len(msgs) > 0:
            if callback != None:
                callback(msgs)
            else:
                print(msgs)

    def do_delete_sms(self, args):
        """ Delete all read and sent SMS messages from phone storage.

        Usage: delete_sms
        """
        DELETE_READ_SENT = b'AT+CMGD=0,2\r' 

        try:
            print("Deleting all read and sent SMS messages.")

            with self.serial_lock:
                # delete all read and sent messages
                self.send_ser_command(DELETE_READ_SENT)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

    def do_init_sms(self, args):
        """ Initialize the SMS Modem once when program launched

        Usage: init_sms
        """

        if self.serial == None:
            self.serial = serial.Serial(self.serial_port, self.serial_rate, write_timeout=2)

        OPERATE_SMS_MODE = b'AT+CMGF=1\r'
        ECHO_MODE = b'ATE1\r'
        ENABLE_MODEM = b'AT+CFUN=1\r' 
        SMS_STORAGE = b'AT+CPMS="MT","MT","MT"\r'
        NO_MESSAGE_INDICATORS = b'AT+CNMI=2,0,0,0,0\r'

        try:
            print("Initializing the SMS modem.")

            with self.serial_lock:
                # Set SMS format to text mode
                self.send_ser_command(OPERATE_SMS_MODE)
                # Set echo mode
                self.send_ser_command(ECHO_MODE)
                # Make sure modem is enabled
                self.send_ser_command(ENABLE_MODEM)
                # Store SMS messages received on the modem
                self.send_ser_command(SMS_STORAGE)
                # Disable unsolicited message indicators
                self.send_ser_command(NO_MESSAGE_INDICATORS)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

'''
    class to add TxTenna functionality to SMS gateway
'''
class goTennaCLI_TxTenna(goTennaCLI, TxTenna):
    def __init__(self, local_gid, local, send_dir, receive_dir, pipe):
        goTennaCLI.__init__(self)
        TxTenna.__init__(self, local_gid, local, send_dir, receive_dir, pipe)
    pass

def run_cli():
    """ The main function of the sample app.

    Instantiates a CLI object and runs it.
    """
    import argparse
    import six

    parser = argparse.ArgumentParser('Run a SMS message goTenna gateway')
    parser.add_argument('SDK_TOKEN', type=six.b,
                        help='The token for the goTenna SDK.')
    parser.add_argument('GEO_REGION', type=six.b,
                        help='The geo region number you are in.')
    parser.add_argument('SERIAL_PORT',
                        help='The serial port of the GSM modem.')
    parser.add_argument('SERIAL_RATE', type=six.b,
                        help='The speed of the serial port of the GSM modem.')
    
    # TxTenna parameters
    parser.add_argument("--gateway", action="store_true",
                        help="Use this computer as an internet connected transaction gateway with a default GID")
    parser.add_argument("--local", action="store_true",
                        help="Use local bitcoind to confirm and broadcast transactions")
    parser.add_argument("--send_dir",
                        help="Broadcast message data from files in this directory")
    parser.add_argument("--receive_dir",
                        help="Write files from received message data in this directory")
    parser.add_argument('-p', '--pipe',
                        default='/tmp/blocksat/api',
                        help='Pipe on which relayed message data is written out to ' +
                        '(default: /tmp/blocksat/api)')                       
    args = parser.parse_args()  

    if (args.gateway):
        cli_obj = goTennaCLI_TxTenna(GATEWAY_GID, args.local, args.send_dir, args.receive_dir, args.pipe)
        cli_obj.txtenna = cli_obj

    else:
        cli_obj = goTennaCLI()

    ## start goTenna SDK thread by setting the SDK token
    cli_obj.do_sdk_token(args.SDK_TOKEN)

    ## set geo region
    cli_obj.do_set_geo_region(args.GEO_REGION)

    cli_obj.do_set_gid(GATEWAY_GID)
    print("set gid=",GATEWAY_GID)

    cli_obj.serial_port = args.SERIAL_PORT
    cli_obj.serial_rate = args.SERIAL_RATE

    try:
        sleep(5)
        cli_obj.cmdloop("Welcome to the SMS Mesh Gateway API sample! "
                        "Press ? for a command list.\n")
    except Exception: # pylint: disable=broad-except
        traceback.print_exc()
        cli_obj.do_quit('')

if __name__ == '__main__':

    run_cli()