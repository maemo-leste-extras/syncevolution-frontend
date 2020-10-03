# This only exists because the real python-conic bindings were broken...

import gconf, dbus, dbus.mainloop.glib

CONNECT_FLAG_NONE = 0
CONNECT_FLAG_AUTOMATICALLY_TRIGGERED = 1 << 0
CON_IC_CONNECT_FLAG_UNMANAGED = 1 << 1

STATUS_CONNECTED = 0
STATUS_DISCONNECTED = 1
STATUS_DISCONNECTING = 2
STATUS_NETWORK_UP = 3

CONNECTION_ERROR_NONE = 0
CONNECTION_ERROR_INVALID_IAP = 1
CONNECTION_ERROR_CONNECTION_FAILED = 2
CONNECTION_ERROR_USER_CANCELED = 3

PROXY_MODE_NONE = 0
PROXY_MODE_MANUAL = 1
PROXY_MODE_AUTO = 2

PROXY_PROTOCOL_HTTP = 0
PROXY_PROTOCOL_HTTPS = 1
PROXY_PROTOCOL_FTP = 2
PROXY_PROTOCOL_SOCKS = 3
PROXY_PROTOCOL_RTSP = 4
_PROTOCOLS = ["http", "https", "ftp", "socks", "rtsp"]

_CONNECTING = 0
_CONNECTED = 1
_DISCONNECTING = 2
_DISCONNECTED = 3

_dbus_service = "com.nokia.icd"
_dbus_path = "/com/nokia/icd"
_dbus_interface = "com.nokia.icd"

_ANY = "[ANY]"

class Event(object):
  def __init__(self, iap, bearer):
    self.iap_id = iap
    self.bearer_type = bearer
  def get_iap_id(self):
    return self.iap_id
  def get_bearer_type(self):
    return self.bearer_type

class ConnectionEvent(Event):
  def __init__(self, iap, bearer, status, error):
    Event.__init__(self, iap, bearer)
    self.status = status
    self.error = error
  def get_status(self):
    return self.status
  def get_error(self):
    return self.error

def _gconf_path(id):
  return "/system/osso/connectivity/IAP/" + gconf.escape_key(id, -1)

class Iap(object):
  def __init__(self, id):
    self.id = id
    # FIXME: check that iap is valid...
  def get_id(self):
    return self.id
  def get_name(self):
    return gconf.client_get_default().get_string(_gconf_path(self.id) + "/name")
  def get_bearer_type(self):
    return gconf.client_get_default().get_string(_gconf_path(self.id) + "/type")

class Connection(object):
  def __init__(self):
    self.bus = dbus.SystemBus(mainloop=dbus.mainloop.glib.DBusGMainLoop())
    self.active = self.bus.name_has_owner(_dbus_service)
    self.connect_cb = None
    self.connect_user = None
    self.status = _DISCONNECTED
    self.iap = None
    self.bearer = None
    # FIXME: register for connection event signals, perhaps, so we know when the connection is lost
  def connect(self, signal, cb, *user_data):
    if signal == "connection-event":
      self.connect_cb = cb
      self.connect_user = user_data
  def request_connection(self, flags):
    return self.request_connection_by_id(_ANY, flags)
  def request_connection_by_id(self, id, flags):
    if not self.active:
      self.handle_connect(id)
      return True
    self.status = _CONNECTING
    # can't get proxies to work for icd, have to resort to this ugliness
    self.bus.call_async(_dbus_service, _dbus_path, _dbus_interface,
                        "connect", "su", (id, flags),
                        self.request_connection_reply, self.request_connection_error, 3*60*1000)
    return True
  def request_connection_reply(self, iap):
    self.handle_connect(iap)
  def request_connection_error(self, error):
    self.status = _DISCONNECTED
    code = CONNECTION_ERROR_CONNECTION_FAILED
    if error == "com.nokia.icd.error.invalid_iap":
      code = CONNECTION_ERROR_INVALID_IAP
    event = ConnectionEvent(None, None, STATUS_DISCONNECTED, code)
    self.connect_cb(self, event, *self.connect_user)
  def handle_connect(self, iap):
    self.status = _CONNECTED
    iap_obj = Iap(iap)
    self.iap = iap_obj.get_id()
    self.bearer = iap_obj.get_bearer_type()
    event = ConnectionEvent(self.iap, self.bearer, STATUS_CONNECTED, CONNECTION_ERROR_NONE)
    self.connect_cb(self, event, *self.connect_user)

  def disconnect(self):
    return self.disconnect_by_id(self.iap)
  def disconnect_by_id(self, id):
    if id is None:
      return False
    if not self.active:
      self.handle_disconnect()
      return True
    self.status = STATUS_DISCONNECTING
    self.bus.call_async(_dbus_service, _dbus_path, _dbus_interface,
                        "disconnect", "s", (id,),
                        self.disconnect_reply, self.disconnect_error, 30*1000)
    return True
  def disconnect_reply(self, iap):
    self.handle_disconnect()
  def disconnect_error(self, error):
    self.handle_disconnect()
  def handle_disconnect(self):
    self.status = _DISCONNECTED
    event = ConnectionEvent(self.iap, self.bearer, STATUS_DISCONNECTED, CONNECTION_ERROR_NONE)
    self.bearer = None
    self.iap = None
    self.connect_cb(self, event, *self.connect_user)

  def _gconf_path(self):
    if self.iap is None:
      return None
    return _gconf_path(self.iap)

  def get_proxy_mode(self):
    if not self.active or self.iap is None:
      return PROXY_MODE_NONE
    mode = gconf.client_get_default().get_string(self._gconf_path() + "/proxytype")
    if mode is None:
      return PROXY_MODE_NONE
    elif mode == "NONE":
      return PROXY_MODE_NONE
    elif mode == "MANUAL":
      return PROXY_MODE_MANUAL
    elif mode == "AUTOCONF":
      return PROXY_MODE_AUTO
    else:
      return PROXY_MODE_NONE
  def get_proxy_host(self, protocol):
    if not self.active or self.iap is None:
      return None
    return gconf.client_get_default().get_string(self._gconf_path() + "/proxy_" + _PROTOCOLS[protocol])
  def get_proxy_port(self, protocol):
    if not self.active or self.iap is None:
      return 0
    return gconf.client_get_default().get_int(self._gconf_path() + "/proxy_" + _PROTOCOLS[protocol] + "_port")
  def get_proxy_autoconfig_url(self):
    if not self.active or self.iap is None:
      return None
    return gconf.client_get_default().get_string(self._gconf_path() + "/autoconf_url")
  def get_proxy_ignore_hosts(self):
    if not self.active or self.iap is None:
      return ""
    return gconf.client_get_default().get_list(self._gconf_path() + "/omit_proxy", gconf.VALUE_STRING)

if __name__ == "__main__":
  conn = Connection()
