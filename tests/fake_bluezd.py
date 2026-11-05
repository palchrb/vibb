#!/usr/bin/env python3
"""Fake org.bluez + org.bluealsa for testing the dbus backend without
hardware (PLAN-bt-dbus.md §8). Runs on a PRIVATE bus:

    dbus-daemon --session --print-address --fork   (or bt_parity.py
    starts one for you) — export the address as DBUS_SYSTEM_BUS_ADDRESS
    so btbus's SystemBus() lands here instead of the real system bus.

Exports:
  /org/bluez            ObjectManager over the fake device tree, plus
                        org.bluez.AgentManager1 (Register/Unregister/
                        RequestDefaultAgent with sender tracking; agents
                        drop when their connection dies, like real bluez)
  /org/bluez/hci0       org.bluez.Adapter1 (Powered/Pairable/Discoverable/
                        DiscoverableTimeout props — stored, NOT emulated:
                        there is deliberately no countdown timer, so a
                        client that forgets to reset Discoverable fails
                        the test instead of being silently rescued —
                        Start/StopDiscovery, SetDiscoveryFilter,
                        RemoveDevice)
  /org/bluez/hci0/dev_* org.bluez.Device1 (Properties incl. RSSI while
                        'discovering'; Pair() calls back into the CALLER's
                        registered agent per SetPairFlow — the deadlock
                        regression: a blocking Pair never answers and
                        fails via the fake's 15s agent timeout)
  /org/bluez/hci0/dev_*/sep1/fd0
                        org.bluez.MediaTransport1 (UUID/State/Device) —
                        the stack-neutral A2DP gate; SetPcm(mac, true)
                        ALSO creates the A2DP-source transport so the
                        parity fixtures keep meaning "audio ready"
  /org/bluealsa         ObjectManager exposing org.bluealsa.PCM1 objects
  /org/vibb/mock      control interface (org.vibb.Mock):
                        AddDevice(mac, name, paired, connected, rssi)
                        SetConnected(mac, bool)  SetPcm(mac, bool)
                        SetTransport(mac, uuid, state)  DropTransport(mac)
                        DropDevice(mac)  SetConnectResult(mac, s)
                        SetPairResult(mac, "verdict [verdict ...]") —
                          queue, one per Pair() call; ok|already|
                          auth-failed|auth-timeout|not-available|
                          in-progress|failed
                        SetPairFlow(mac, "just-works"|"confirm"|"pin")
                        SetPairingMode(mac, bool) — a removed device in
                          pairing mode re-appears on next StartDiscovery
                        SimulateIncomingPair(mac) -> "paired"|"rejected"|
                          "no-default-agent" — drives the DEFAULT agent
                          like a car head unit
                        GetTrusted/GetConnected/GetConnectCount,
                        GetPairCount/GetRemoveCount, GetDiscoverable,
                        GetDiscoverableTimeout, GetAgentEvents

Requires python3-dbus + python3-gi (present on the rig, apt on dev
machines). Existing Mock signatures are FROZEN — bt_parity/bt_actions/
bt_reconnect drive them via dbus-send; grow additively only.
"""

import os
import sys

import dbus
import dbus.bus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)
_ADDR = os.environ.get("DBUS_SYSTEM_BUS_ADDRESS")
# explicit connection: never risk landing on the real system bus
BUS = dbus.bus.BusConnection(_ADDR) if _ADDR else dbus.SystemBus()

DEVICES = {}   # mac -> {name, paired, connected, rssi}
DEVICE_OBJS = {}  # mac -> Device (needed to emit PropertiesChanged)
PCMS = {}      # mac -> bool
TRANSPORTS = {}  # mac -> {"uuid", "state"} (org.bluez.MediaTransport1)
A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
DISCOVERING = [False]
ROOT = [None]  # BluezRoot instance (emits InterfacesAdded)

# phase B state (PLAN-bt-b2-pairing.md §4)
ADAPTER = {"Powered": True, "Pairable": True, "Discoverable": False,
           "DiscoverableTimeout": 0}
AGENTS = {}          # sender unique name -> {"path", "caps"}
DEFAULT_AGENT = [None]   # (sender, path) or None
EVENTS = []          # ordered agent-interaction log (GetAgentEvents)
REMOVED = {}         # mac -> device dict stash (revived by discovery
                     # when flagged pairing_mode — like a real speaker)
REMOVES = {}         # mac -> RemoveDevice count (survives the removal)


def dev_path(mac):
    return "/org/bluez/hci0/dev_" + mac.upper().replace(":", "_")


def device_props(mac):
    d = DEVICES[mac]
    props = {
        "Address": mac.upper(),
        "Alias": d["name"],
        "Paired": dbus.Boolean(d["paired"]),
        "Connected": dbus.Boolean(d["connected"]),
        "Trusted": dbus.Boolean(d.get("trusted", False)),
        "UUIDs": dbus.Array(d.get("uuids", []), signature="s"),
    }
    if DISCOVERING[0] and d.get("rssi") is not None:
        props["RSSI"] = dbus.Int16(d["rssi"])
    return props


class BluezRoot(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.ObjectManager",
                         out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {"/org/bluez/hci0": {"org.bluez.Adapter1": {
            "Powered": dbus.Boolean(True),
            "Pairable": dbus.Boolean(True)}}}
        for mac in DEVICES:
            objs[dev_path(mac)] = {"org.bluez.Device1": device_props(mac)}
        for mac, tr in TRANSPORTS.items():
            objs[dev_path(mac) + "/sep1/fd0"] = {"org.bluez.MediaTransport1": {
                "Device": dbus.ObjectPath(dev_path(mac)),
                "UUID": tr["uuid"], "State": tr["state"],
                "Codec": dbus.Byte(0)}}
        return objs

    @dbus.service.signal("org.freedesktop.DBus.ObjectManager",
                         signature="oa{sa{sv}}")
    def InterfacesAdded(self, path, ifaces):
        pass


def _adapter_variant(prop):
    v = ADAPTER.get(str(prop), False)
    return (dbus.UInt32(v) if prop == "DiscoverableTimeout"
            else dbus.Boolean(bool(v)))


class Adapter(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return _adapter_variant(str(prop))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return {p: _adapter_variant(p) for p in ADAPTER}

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ssv")
    def Set(self, iface, prop, value):
        # stored, never expired: DiscoverableTimeout has NO countdown by
        # design — the client must prove it resets Discoverable itself
        prop = str(prop)
        ADAPTER[prop] = (int(value) if prop == "DiscoverableTimeout"
                         else bool(value))
        self.PropertiesChanged("org.bluez.Adapter1",
                               {prop: _adapter_variant(prop)}, [])

    @dbus.service.signal("org.freedesktop.DBus.Properties",
                         signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    @dbus.service.method("org.bluez.Adapter1", in_signature="a{sv}")
    def SetDiscoveryFilter(self, filt):
        pass

    @dbus.service.method("org.bluez.Adapter1")
    def StartDiscovery(self):
        DISCOVERING[0] = True
        # a removed device in pairing mode makes itself seen again —
        # exactly what the stale-key clear-and-retry flow depends on
        for mac in list(REMOVED):
            if REMOVED[mac].get("pairing_mode"):
                DEVICES[mac] = REMOVED.pop(mac)
                if mac not in DEVICE_OBJS:
                    DEVICE_OBJS[mac] = Device(mac)
                ROOT[0].InterfacesAdded(
                    dbus.ObjectPath(dev_path(mac)),
                    {"org.bluez.Device1": device_props(mac)})

    @dbus.service.method("org.bluez.Adapter1")
    def StopDiscovery(self):
        DISCOVERING[0] = False

    @dbus.service.method("org.bluez.Adapter1", in_signature="o")
    def RemoveDevice(self, path):
        for mac in list(DEVICES):
            if dev_path(mac) == str(path):
                REMOVES[mac] = REMOVES.get(mac, 0) + 1
                REMOVED[mac] = DEVICES.pop(mac)
                return
        raise _bluez_error("DoesNotExist", "Does Not Exist")


class AgentManager(dbus.service.Object):
    """org.bluez.AgentManager1 at /org/bluez, with real-bluez semantics:
    agents are per-connection (sender-tracked) and vanish when the owning
    connection dies (NameOwnerChanged watch installed in main())."""

    @dbus.service.method("org.bluez.AgentManager1", in_signature="os",
                         sender_keyword="sender")
    def RegisterAgent(self, path, caps, sender=None):
        if str(sender) in AGENTS:
            raise _bluez_error("AlreadyExists", "Already Exists")
        AGENTS[str(sender)] = {"path": str(path), "caps": str(caps)}
        EVENTS.append(f"Register:{caps}")

    @dbus.service.method("org.bluez.AgentManager1", in_signature="o",
                         sender_keyword="sender")
    def UnregisterAgent(self, path, sender=None):
        if str(sender) not in AGENTS:
            raise _bluez_error("DoesNotExist", "Does Not Exist")
        AGENTS.pop(str(sender))
        if DEFAULT_AGENT[0] and DEFAULT_AGENT[0][0] == str(sender):
            DEFAULT_AGENT[0] = None
        EVENTS.append("Unregister")

    @dbus.service.method("org.bluez.AgentManager1", in_signature="o",
                         sender_keyword="sender")
    def RequestDefaultAgent(self, path, sender=None):
        if str(sender) not in AGENTS:
            raise _bluez_error("DoesNotExist", "Does Not Exist")
        DEFAULT_AGENT[0] = (str(sender), str(path))
        EVENTS.append("RequestDefaultAgent")


def _agent_iface(sender, path):
    # introspect=False: a deadlocked client can't answer Introspect either,
    # and a blocking introspection here would stall the whole fake
    return dbus.Interface(BUS.get_object(sender, path, introspect=False),
                          "org.bluez.Agent1")


def _bluez_error(name, msg):
    e = dbus.exceptions.DBusException(msg)
    e._dbus_error_name = "org.bluez.Error." + name
    return e


class Device(dbus.service.Object):
    def __init__(self, mac):
        self.mac = mac
        super().__init__(BUS, dev_path(mac))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return device_props(self.mac)

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return device_props(self.mac).get(prop, dbus.Boolean(False))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ssv")
    def Set(self, iface, prop, value):
        if prop == "Trusted":
            DEVICES[self.mac]["trusted"] = bool(value)

    @dbus.service.signal("org.freedesktop.DBus.Properties",
                         signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    def set_connected(self, connected):
        DEVICES[self.mac]["connected"] = connected
        self.PropertiesChanged("org.bluez.Device1",
                               {"Connected": dbus.Boolean(connected)}, [])

    @dbus.service.method("org.bluez.Device1")
    def Connect(self):
        DEVICES[self.mac]["connects"] = DEVICES[self.mac].get("connects", 0) + 1
        result = DEVICES[self.mac].get("connect_result", "ok")
        if result == "ok":
            self.set_connected(True)
            return
        if result == "already-connected":
            raise _bluez_error("AlreadyConnected", "Already Connected")
        raise _bluez_error("Failed", "br-connection-page-timeout")

    @dbus.service.method("org.bluez.Device1")
    def Disconnect(self):
        if not DEVICES[self.mac]["connected"]:
            raise _bluez_error("NotConnected", "Not Connected")
        self.set_connected(False)

    def _finish_pair(self, verdict, ok_cb, err_cb):
        if verdict == "ok":
            DEVICES[self.mac]["paired"] = True
            self.PropertiesChanged("org.bluez.Device1",
                                   {"Paired": dbus.Boolean(True)}, [])
            ok_cb()
        else:
            name, msg = {
                "already": ("AlreadyExists", "Already Exists"),
                "auth-failed": ("AuthenticationFailed",
                                "Authentication Failed"),
                "auth-timeout": ("AuthenticationTimeout",
                                 "Authentication Timeout"),
                "not-available": ("ConnectionAttemptFailed", "Page Timeout"),
                "in-progress": ("InProgress", "In Progress"),
            }.get(verdict, ("Failed", "failed"))
            err_cb(_bluez_error(name, msg))

    @dbus.service.method("org.bluez.Device1", sender_keyword="sender",
                         async_callbacks=("ok_cb", "err_cb"))
    def Pair(self, sender=None, ok_cb=None, err_cb=None):
        """Async so the fake keeps dispatching while it waits on the
        CALLER's agent — which is what makes a deadlocked (blocking)
        client observable: its agent never answers, the 15s reply
        timeout below fires, and Pair fails fast instead of hanging."""
        d = DEVICES[self.mac]
        d["pairs"] = d.get("pairs", 0) + 1
        queue = d.setdefault("pair_results", [])
        verdict = queue.pop(0) if queue else "ok"
        flow = d.get("pair_flow", "just-works")
        if flow == "just-works":  # JBL-class: no agent callback at all
            self._finish_pair(verdict, ok_cb, err_cb)
            return
        agent = AGENTS.get(str(sender))
        if not agent:
            err_cb(_bluez_error("Failed", "no agent registered by caller"))
            return
        iface = _agent_iface(str(sender), agent["path"])
        path = dbus.ObjectPath(dev_path(self.mac))

        def agent_failed(e):
            timeout = "NoReply" in (e.get_dbus_name() or "")
            EVENTS.append("AgentTimeout" if timeout else "AgentError")
            self._finish_pair("auth-timeout" if timeout else "auth-failed",
                              ok_cb, err_cb)

        if flow == "pin":
            def got_pin(pin):
                EVENTS.append(f"RequestPinCode:answered:{pin}")
                if str(pin) == "0000":
                    self._finish_pair(verdict, ok_cb, err_cb)
                else:
                    err_cb(_bluez_error("AuthenticationFailed",
                                        f"bad pin {pin}"))
            iface.RequestPinCode(path, reply_handler=got_pin,
                                 error_handler=agent_failed, timeout=15)
        else:  # confirm
            def confirmed():
                EVENTS.append("RequestConfirmation:answered")
                self._finish_pair(verdict, ok_cb, err_cb)
            iface.RequestConfirmation(path, dbus.UInt32(123456),
                                      reply_handler=confirmed,
                                      error_handler=agent_failed,
                                      timeout=15)


class BluealsaRoot(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.ObjectManager",
                         out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {}
        for mac, present in PCMS.items():
            if present:
                path = ("/org/bluealsa/hci0/dev_"
                        + mac.upper().replace(":", "_") + "/a2dpsrc/sink")
                objs[path] = {"org.bluealsa.PCM1": {
                    "Device": dbus.ObjectPath(dev_path(mac)),
                    "Mode": "sink", "Transport": "A2DP-source"}}
        return objs


class Mock(dbus.service.Object):
    @dbus.service.method("org.vibb.Mock", in_signature="ssbbn")
    def AddDevice(self, mac, name, paired, connected, rssi):
        mac = str(mac).upper()
        DEVICES[mac] = {"name": str(name), "paired": bool(paired),
                        "connected": bool(connected),
                        "rssi": int(rssi) if int(rssi) != 0 else None}
        if mac not in DEVICE_OBJS:  # re-adds must not re-export the path
            DEVICE_OBJS[mac] = Device(mac)
        ROOT[0].InterfacesAdded(dbus.ObjectPath(dev_path(mac)),
                                {"org.bluez.Device1": device_props(mac)})

    @dbus.service.method("org.vibb.Mock", in_signature="sb")
    def SetConnected(self, mac, connected):
        # state change + PropertiesChanged, like real bluez
        DEVICE_OBJS[str(mac).upper()].set_connected(bool(connected))

    @dbus.service.method("org.vibb.Mock", in_signature="sb")
    def SetPcm(self, mac, present):
        mac = str(mac).upper()
        PCMS[mac] = bool(present)
        # real bluealsa only has a PCM once BlueZ configured the transport
        if present:
            TRANSPORTS[mac] = {"uuid": A2DP_SOURCE, "state": "idle"}
        else:
            TRANSPORTS.pop(mac, None)

    @dbus.service.method("org.vibb.Mock", in_signature="sss")
    def SetTransport(self, mac, uuid, state):
        TRANSPORTS[str(mac).upper()] = {"uuid": str(uuid), "state": str(state)}

    @dbus.service.method("org.vibb.Mock", in_signature="s")
    def DropTransport(self, mac):
        TRANSPORTS.pop(str(mac).upper(), None)

    @dbus.service.method("org.vibb.Mock", in_signature="s")
    def DropDevice(self, mac):
        DEVICES.pop(str(mac).upper(), None)

    @dbus.service.method("org.vibb.Mock", in_signature="ss")
    def SetConnectResult(self, mac, result):
        # 'ok' | 'already-connected' | 'failed'
        DEVICES[str(mac).upper()]["connect_result"] = str(result)

    @dbus.service.method("org.vibb.Mock", in_signature="ss")
    def SetUuids(self, mac, uuids):
        # space-separated profile UUIDs (adopt checks for Audio Sink)
        DEVICES[str(mac).upper()]["uuids"] = str(uuids).split()

    @dbus.service.method("org.vibb.Mock", in_signature="s",
                         out_signature="b")
    def GetTrusted(self, mac):
        return bool(DEVICES[str(mac).upper()].get("trusted", False))

    @dbus.service.method("org.vibb.Mock", in_signature="s",
                         out_signature="b")
    def GetConnected(self, mac):
        return bool(DEVICES[str(mac).upper()]["connected"])

    @dbus.service.method("org.vibb.Mock", in_signature="s",
                         out_signature="i")
    def GetConnectCount(self, mac):
        return int(DEVICES[str(mac).upper()].get("connects", 0))

    # --- phase B controls (PLAN-bt-b2-pairing.md §4) -------------------------

    @dbus.service.method("org.vibb.Mock", in_signature="ss")
    def SetPairResult(self, mac, results):
        # space-separated queue, one verdict per Pair() call; exhausted
        # queue means "ok" — so "auth-failed ok" IS the stale-key story
        DEVICES[str(mac).upper()]["pair_results"] = str(results).split()

    @dbus.service.method("org.vibb.Mock", in_signature="ss")
    def SetPairFlow(self, mac, flow):
        # 'just-works' | 'confirm' | 'pin'
        DEVICES[str(mac).upper()]["pair_flow"] = str(flow)

    @dbus.service.method("org.vibb.Mock", in_signature="sb")
    def SetPairingMode(self, mac, on):
        mac = str(mac).upper()
        tgt = DEVICES.get(mac) or REMOVED.get(mac)
        tgt["pairing_mode"] = bool(on)

    @dbus.service.method("org.vibb.Mock", in_signature="s",
                         out_signature="s",
                         async_callbacks=("ok_cb", "err_cb"))
    def SimulateIncomingPair(self, mac, ok_cb=None, err_cb=None):
        """Drive the DEFAULT agent like a car head unit would. The
        'no-default-agent' return IS an assertion: outside a visible
        window the box must not be silently pairable."""
        mac = str(mac).upper()
        if DEFAULT_AGENT[0] is None:
            ok_cb("no-default-agent")
            return
        if mac not in DEVICES:  # a car is a never-before-seen MAC
            DEVICES[mac] = {"name": "Fake Car", "paired": False,
                            "connected": False, "rssi": None}
        if mac not in DEVICE_OBJS:
            DEVICE_OBJS[mac] = Device(mac)
        sender, apath = DEFAULT_AGENT[0]
        iface = _agent_iface(sender, apath)
        path = dbus.ObjectPath(dev_path(mac))

        def rejected(_e):
            EVENTS.append("IncomingRejected")
            ok_cb("rejected")

        def complete():
            DEVICES[mac]["paired"] = True
            DEVICE_OBJS[mac].PropertiesChanged(
                "org.bluez.Device1", {"Paired": dbus.Boolean(True)}, [])
            ROOT[0].InterfacesAdded(path,
                                    {"org.bluez.Device1": device_props(mac)})
            ok_cb("paired")

        def authorize():
            iface.AuthorizeService(
                path, "0000110a-0000-1000-8000-00805f9b34fb",
                reply_handler=lambda: (
                    EVENTS.append("AuthorizeService:answered"), complete()),
                error_handler=rejected, timeout=15)

        if DEVICES[mac].get("pair_flow") == "confirm":
            iface.RequestConfirmation(
                path, dbus.UInt32(654321),
                reply_handler=lambda: (
                    EVENTS.append("RequestConfirmation:answered"),
                    authorize()),
                error_handler=rejected, timeout=15)
        else:
            authorize()

    @dbus.service.method("org.vibb.Mock", in_signature="s",
                         out_signature="i")
    def GetPairCount(self, mac):
        mac = str(mac).upper()
        d = DEVICES.get(mac) or REMOVED.get(mac) or {}
        return int(d.get("pairs", 0))

    @dbus.service.method("org.vibb.Mock", in_signature="s",
                         out_signature="i")
    def GetRemoveCount(self, mac):
        return int(REMOVES.get(str(mac).upper(), 0))

    @dbus.service.method("org.vibb.Mock", out_signature="b")
    def GetDiscoverable(self):
        return bool(ADAPTER["Discoverable"])

    @dbus.service.method("org.vibb.Mock", out_signature="u")
    def GetDiscoverableTimeout(self):
        return int(ADAPTER["DiscoverableTimeout"])

    @dbus.service.method("org.vibb.Mock", out_signature="as")
    def GetAgentEvents(self):
        return EVENTS


_NAMES = []  # keep references — dbus-python RELEASES a bus name when
             # its BusName object is garbage-collected


def _owner_changed(name, _old, new):
    """Real bluez drops an agent when its connection dies — mirror that,
    and log it, so the killed-mid-window test can observe the cleanup."""
    name = str(name)
    if str(new) == "" and name in AGENTS:
        AGENTS.pop(name, None)
        if DEFAULT_AGENT[0] and DEFAULT_AGENT[0][0] == name:
            DEFAULT_AGENT[0] = None
        EVENTS.append("OwnerGone")


def main():
    for name in ("org.bluez", "org.bluealsa"):
        _NAMES.append(dbus.service.BusName(name, BUS))
    ROOT[0] = BluezRoot(BUS, "/")  # real bluez: ObjectManager at the root
    Adapter(BUS, "/org/bluez/hci0")
    AgentManager(BUS, "/org/bluez")  # real bluez: AgentManager1 here
    BluealsaRoot(BUS, "/org/bluealsa")
    Mock(BUS, "/org/vibb/mock")
    BUS.add_signal_receiver(_owner_changed, signal_name="NameOwnerChanged",
                            dbus_interface="org.freedesktop.DBus")
    print("fake-bluezd ready", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    sys.exit(main())
