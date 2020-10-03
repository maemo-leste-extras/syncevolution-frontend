#!/usr/bin/python2.5
import sys, os, fcntl, gtk, gobject, hildon, osso, dbus, signal, alarm, time
import pyconic; conic=pyconic # ideally, import conic, but that's broken
import syncevolution

GUI_VERBOSITY = 0

THUMB_SIZE = gtk.HILDON_SIZE_AUTO_WIDTH | gtk.HILDON_SIZE_THUMB_HEIGHT
FINGER_SIZE = gtk.HILDON_SIZE_AUTO_WIDTH | gtk.HILDON_SIZE_FINGER_HEIGHT

TIMEFMT = "%x %X" # should perhaps make this configurable

default_sources = [
  ("addressbook", "Contacts"),
  ("calendar", "Calendar"),
  ("todo", "Tasks"),
  ("memo", "Notes"),
  ("calendar+todo", "Calendar/Tasks"),
]

backend_aliases = {
  # syncevolution backends used on Maemo
  "evolution-contacts": "addressbook",
  "maemo-events": "calendar",
  "maemo-tasks": "todo",
  "maemo-notes": "memo",
}

template_names = {
  "Google": "Google Contacts",
}

calendar_names = {
  "cal_ti_calendar_private": "Private",
  "cal_ti_smart_birthdays": "Birthdays",
}

dav_template = "_DAV_"
dav_sources = ["addressbook", "calendar"]
dav_config = {"peerType": "WebDAV",
  "sources": {"addressbook": {"sync": "two-way", "backend": "CardDAV", "uri": "addressbook"},
              "calendar":    {"sync": "two-way", "backend": "CalDAV",  "uri": "calendar"}}
}

default_modes = [
  # When the user selects Normal Sync, the mode configured in the sync sources will be used.
  # (Each sync source can have its own mode, independent of the other sources.)
  # Could perhaps have a separate "Default" for that, but that might be confusing,
  # and a "Normal Sync" option that enforces two-way sync even for sync sources where only
  # one-way is configured seems like a bad idea anyway.
  # (Standard, Reversed, Description)
  (None, None, "Normal Sync"),
  ("slow", "slow", "Slow Sync"),
  ("refresh-from-client", "refresh-from-server", "Refresh From Client"),
  ("refresh-from-server", "refresh-from-client", "Refresh From Server"),
  ("one-way-from-client", "one-way-from-server", "One-Way From Client"),
  ("one-way-from-server", "one-way-from-client", "One-Way From Server"),
]

source_modes = [
  # (Standard, Reversed, Description)
  ("disabled", "disabled", "Disabled"),
  ("two-way", "two-way", "Normal Sync"),
  ("slow", "slow", "Slow Sync"),
  ("refresh-from-client", "refresh-from-server", "Refresh From Client"),
  ("refresh-from-server", "refresh-from-client", "Refresh From Server"),
  ("one-way-from-client", "one-way-from-server", "One-Way From Client"),
  ("one-way-from-server", "one-way-from-client", "One-Way From Server"),
]

RESULT_NO_CONNECTION = -2
RESULT_LOCKED = -3
RESULT_ABORTED = -4

def get_method(url):
  pos = url.find(":")
  if pos < 0:
    return None
  return url[:pos]

class SyncRunner(object):
  def __init__(self, auto, sync, server, dir, config, mode=None, sources=None):
    self.auto = auto
    self.sync = sync
    self.server = server
    self.dir = dir
    self.config = config
    self.mode = mode
    self.sources = sources
    self.state = None
    self.sync_watches = None
    self.connection = None
    self.conn_status = None
    self.aborted = None
    self.poll_cb = None
    self.tick_cb = None
    # determine connection method
    url = config.get("syncURL")
    self.method = get_method(url)
    # grab exclusive lock
    self.acquire_lock()
    # connect to network
    self.connection = conic.Connection()
    flags = conic.CONNECT_FLAG_NONE
    if self.auto:
      flags |= conic.CONNECT_FLAG_AUTOMATICALLY_TRIGGERED
    self.connection.connect("connection-event", self.connected)
    self.connection.request_connection(flags)
  def acquire_lock(self):
    lock_name = self.dir + "/.sync_lock"
    tries = 0
    while True:
      try:
        fd = os.open(lock_name, os.O_RDWR|os.O_CREAT|os.O_EXCL)
        try:
          fcntl.flock(fd, fcntl.LOCK_EX)
        except:
          # since we're blocking, this shouldn't happen, in theory
          os.close(fd)
          continue
        break
      except:
        # check if this might be a stale lock
        try:
          fd = os.open(lock_name, os.O_RDWR)
        except:
          # file vanished, try to lock again
          continue
        try:
          fcntl.flock(fd, fcntl.LOCK_EX)
        except:
          # since we're blocking, this shouldn't happen, in theory
          os.close(fd)
          continue
        buf = os.read(fd, 256)
        if buf == "":
          # another process might be in the process of locking
          # (opened the file but didn't lock it yet)
          if tries > 10:
            # perhaps not, guess we'll take the lock instead
            break
          else:
            tries += 1
          os.close(fd)
          continue
        pid = int(buf)
        # since the file isn't empty, and we have the lock,
        # the other process must have terminated prematurely.
        # Take over the lock.
        os.lseek(fd, 0, 0)
        os.ftruncate(fd, 0)
        break
    self.lock_fd = fd
    pid = os.getpid()
    os.write(fd, "%d" % pid)
  def release_lock(self):
    os.unlink(self.dir + "/.sync_lock")
    os.close(self.lock_fd)
  def connected(self, connection, event):
    self.conn_status = event.get_status()
    if self.aborted:
      return
    if self.conn_status == conic.STATUS_CONNECTED:
      # we're connected, see if the current connection is using proxies
      proxy_mode = self.connection.get_proxy_mode()
      if proxy_mode == conic.PROXY_MODE_NONE:
        proxy = None
      elif proxy_mode == conic.PROXY_MODE_MANUAL:
        if self.method == "http":
          proto = conic.PROXY_PROTOCOL_HTTP
        elif self.method == "https":
          proto = conic.PROXY_PROTOCOL_HTTPS
        else: # hmm... just default to http?
          proto = conic.PROXY_PROTOCOL_HTTP
        proxy_host = self.connection.get_proxy_host(proto)
        proxy_port = self.connection.get_proxy_port(proto)
        proxy = self.method + "://%s:%d" % (proxy_host, proxy_port)
      elif proxy_mode == conic.PROXY_MODE_AUTO:
        print "Can't handle automatic proxy yet!"
        proxy = None
      # all set, launch SyncEvolution
      self.state = self.sync.synchronize_start(self.server, self.config, self.mode, self.sources, proxy)
      if not (self.poll_cb is None and self.tick_cb is None):
        self.activate_watches()
    elif not self.poll_cb is None:
      self.poll_cb(None, None)
  def set_callbacks(self, poll_cb, tick_cb=None):
    self.poll_cb = poll_cb
    self.tick_cb = tick_cb
    if self.connect_error():
      self.poll_cb(None, None)
      return
    if not self.state is None:
      self.activate_watches()
  def connect_error(self):
    if self.connection is None:
      return True
    if self.aborted and not self.state:
      return True
    if self.conn_status is None or self.conn_status == conic.STATUS_CONNECTED:
      return False
    return True
  def activate_watches(self):
    if not self.sync_watches is None:
      self.deactivate_watches()
    self.sync_watches = [gobject.io_add_watch(w, gobject.IO_IN|gobject.IO_HUP, self.poll_cb) for w in self.state.watches()]
    if not self.tick_cb is None:
      self.sync_watches.append(gobject.timeout_add(100, self.tick_cb))
  def deactivate_watches(self):
    if self.sync_watches is None:
      return
    for w in self.sync_watches:
      gobject.source_remove(w)
    self.sync_watches = None
  def __del__(self):
    if not self.state is None:
      self.finish()
  def poll(self):
    if self.state is None:
      return self.connect_error()
    return self.state.poll()
  def progress(self):
    if self.state is None:
      return None
    return self.state.progress()
  def abort(self):
    self.aborted = True
    if self.state is None:
      return
    self.state.abort()
  def finish(self):
    # avoid recursive calls from cleanup
    self.poll_cb = None
    self.tick_cb = None
    if self.state is None:
      status = {}
      if self.connection is None:
        status["result"] = RESULT_LOCKED
      else:
        if self.aborted:
          status["result"] = RESULT_ABORTED
        else:
          status["result"] = RESULT_NO_CONNECTION
        self.release_lock()
      return status
    self.deactivate_watches()
    status = self.sync.synchronize_status(self.state)
    if self.aborted and status["result"]:
      status["result"] = RESULT_ABORTED
    self.connection.disconnect()
    self.state = None
    self.release_lock()
    return status

class SyncGUI(object):
  def __init__(self, quiet=False):
    gtk.set_application_name("SyncEvolution")
    self.program = hildon.Program.get_instance()
    self.sync = syncevolution.SyncEvolution(quiet)
    self.dbus = dbus.SessionBus()
    self.server = None
    self.server_name = None
    self.server_dir = None
    self.server_config = None
    self.server_cookie = None
    self.server_sessions = None
    self.main_window = None
    self.server_window = None
    self.history_window = None
    self.wizard_state = None
    self.sync_dialog = None
    self.sync_mode = None
    self.sync_sources = None
    self.sync_progress = None
    self.sync_bar = None
    self.sync_state = None
    self.sync_runner = None
    self.servers = None
    self.serverlist = None
    self.selector = None
    self.init_main()
  def main(self):
    gtk.main()

### MAIN WINDOW
  def get_main_title(self):
    return "Sync services"
  def init_main(self):
    self.main_window = hildon.StackableWindow()
    self.main_window.set_title(self.get_main_title())
    self.main_window.connect("destroy", self.destroy_main)
    self.program.add_window(self.main_window)

    menu = hildon.AppMenu()
    button = hildon.GtkButton(gtk.HILDON_SIZE_AUTO)
    button.set_label("Add new service")
    button.connect("clicked", self.create_server)
    menu.append(button)
    button = hildon.GtkButton(gtk.HILDON_SIZE_AUTO)
    button.set_label("Delete service")
    button.connect("clicked", self.delete_servers)
    menu.append(button)
    menu.show_all()
    self.main_window.set_app_menu(menu)

    self.init_main_selector()
    self.main_window.show()
  def init_main_selector(self):
    if not self.selector is None:
      self.selector.destroy()

    self.servers = self.sync.get_servers()
    self.serverlist = []
    for server in self.servers:
      st = server.split("@", 1)
      if len(st) < 2:
        st.append("default")
      # The SyncEvolution authors suggest using the configuration
      # "consumerReady=0" to hide server configurations that should
      # not be available for synchronization from GUIs. This is used
      # to hide the source configuration for CardDAV/CalDAV backends.
      # However, these same configs also normally get a special name,
      # "target-config", which is way easier to check, so for now,
      # I'll just hide these configs based on their name.
      if st[0] == "target-config":
        continue
      self.serverlist.append(st)
    self.selector = hildon.TouchSelector(text = True)
    self.selector.set_hildon_ui_mode("normal")
    self.selector.connect("changed", self.server_selected)
    self.init_selector(self.selector)
    self.selector.show()
    self.main_window.add(self.selector)
  def init_selector(self, selector):
    names = {}
    for srv, ctx in self.serverlist:
      names.setdefault(srv, []).append(ctx)
    for srv, ctx in self.serverlist:
      if len(names[srv]) > 1:
        selector.append_text("%s [%s]" % (srv, ctx))
      else:
        selector.append_text(srv)
  def destroy_main(self, window):
    gtk.main_quit()
  def server_selected(self, selector, column):
    row = selector.get_last_activated_row(column)[0]
    server = self.serverlist[row]
    if server[1] == "default":
      self.server = server[0]
    else:
      self.server = "%s@%s" % (server[0], server[1])
    self.server_name = server[0]
    self.server_dir = self.sync.get_server_dir(self.server)
    self.server_config = self.sync.get_server_config(self.server)
    self.load_cookie()
    self.init_server_config()
  def create_server(self, button):
    self.init_server_create()
  def delete_servers(self, button):
    self.init_server_delete()
  def load_server_cookie(self, server_dir):
    try:
      return int(open(server_dir + "/.alarmcookie", "r").read())
    except:
      return None
  def load_cookie(self):
    self.server_cookie = self.load_server_cookie(self.server_dir)
### SERVER DELETION
  def init_server_delete(self):
    window = hildon.StackableWindow()
    toolbar = hildon.EditToolbar("Delete services", "Delete")
    window.set_edit_toolbar(toolbar)
    selector = hildon.TouchSelector(text = True)
    window.add(selector)
    self.init_selector(selector)
    selector.set_column_selection_mode(hildon.TOUCH_SELECTOR_SELECTION_MODE_MULTIPLE)
    selector.unselect_all(0)
    toolbar.connect("button-clicked", self.server_delete_clicked, window, selector)
    toolbar.connect("arrow-clicked", self.server_delete_close, window)
    window.show_all()
    window.fullscreen()
  def server_delete_clicked(self, button, window, selector):
    # for some reason, the python bindings don't seem to have get_selected_rows,
    # so I'll have to use this hack, parsing the string
    server_list = selector.get_current_text()[1:-1]
    if server_list == "":
      servers = []
    else:
      servers = server_list.split(",")
    # ask for confirmation
    if len(servers) == 0:
      banner = hildon.hildon_banner_show_information(window, "", "No services selected")
      response = gtk.RESPONSE_DELETE_EVENT
    elif len(servers) == 1:
      note = hildon.hildon_note_new_confirmation(window, "Delete service %s?" % servers[0])
      response = gtk.Dialog.run(note)
      note.destroy()
    else:
      note = hildon.hildon_note_new_confirmation(window, "Delete selected services?")
      response = gtk.Dialog.run(note)
      note.destroy()
    if response == gtk.RESPONSE_OK:
      names = {}
      contexts = {}
      for srv, ctx in self.serverlist:
        names.setdefault(srv, []).append(ctx)
        contexts.setdefault(ctx, []).append(srv)
      for server in servers:
        # more mangling because we don't have get_selected_rows
        context = None
        name = server
        if server[-1] == "]":
          cpos = server.find(" [")
          if cpos >= 0:
            name = server[:cpos]
            context = server[cpos+2:-1]
        if context is None:
          if len(names[name]) > 0:
            context = names[name][0]
        if context is not None and context != "default":
          server = "%s@%s" % (name, context)
        self.do_delete_server(server)
        # if all servers in a context were deleted,
        # delete context as well
        if context is not None:
          contexts[context].remove(name)
          if len(contexts[context]) == 0:
            self.do_delete_context(context)
            del contexts[context]
      self.server_delete_close(button, window)
      self.init_main_selector()
  def server_delete_close(self, button, window):
    window.destroy()
  def do_delete_server(self, server):
    server_dir = self.sync.get_server_dir(server)
    server_cookie = self.load_server_cookie(server_dir)
    server_config = self.sync.get_server_config(server)
    if not server_cookie is None:
      os.unlink(server_dir + "/.alarmcookie")
      alarm.delete_event(server_cookie)
    self.sync.delete_server(server)
    if self.sync_is_local(server_config) != 0:
      url = server_config.get("syncURL")
      self.sync.delete_context(url[9:])
  def do_delete_context(self, context):
    if not self.sync.has_contexts():
      return
    self.sync.delete_context(context)

### SERVER CREATION
  def init_server_create(self):
    self.wizard_state = {}

    has_contexts = self.sync.has_contexts()

    std_backends = self.sync.get_backends()

    std_templates = self.sync.get_templates()
    if len(std_templates) and std_templates[0] == "template name":
      del std_templates[0]
    std_templates.sort()

    templates = []
    if "SyncEvolution" in std_templates:
      std_templates.remove("SyncEvolution")
      templates.append(("SyncEvolution", "Generic SyncML", True))
    else:
      templates.append(("default", "Generic SyncML", True))
    if "WebDAV" in std_templates:
      std_templates.remove("WebDAV")
      templates.append(("WebDAV", "Generic DAV", True))
    elif std_backends.has_key("CardDAV") or std_backends.has_key("CalDAV"):
      templates.append((dav_template, "Generic DAV", True))

    # FIXME: perhaps also check for new ActiveSync template

    for label in std_templates:
      templates.append((label, template_names.get(label, label.replace("_", " ")), False))

    self.wizard_state["templates"] = templates

    notebook = gtk.Notebook()

    label = gtk.Label("This wizard will walk you through configuring a new synchronization service.\n"
                      "Tap 'Next' to continue.")
    label.set_line_wrap(True)
    notebook.append_page(label, None)

    # Page 2
    vbox = gtk.VBox(False, 0)

    servername = hildon.Entry(gtk.HILDON_SIZE_AUTO)
#    servername.set_placeholder("Service Name")
    self.wizard_state["name"] = servername
    caption = hildon.Caption(None, "Service Name", servername)
    caption.set_status(hildon.CAPTION_MANDATORY)
    vbox.pack_start(caption, False, False, 0)

    selector = hildon.TouchSelector(text = True)
    for name, label, generic in templates:
      selector.append_text(label)
    selector.set_active(0, 0)
    self.wizard_state["template"] = selector

    button = hildon.PickerButton(FINGER_SIZE, hildon.BUTTON_ARRANGEMENT_HORIZONTAL)
    button.set_alignment(0, 0.5, 0.5, 0.5)
    button.set_title("Template")
    button.set_selector(selector)
    vbox.pack_start(button, False, False, 0)

    if has_contexts:
      contextname = hildon.Entry(gtk.HILDON_SIZE_AUTO)
      contextname.set_placeholder("Optional. For advanced configurations.")
      self.wizard_state["context"] = contextname
      caption = hildon.Caption(None, "Context", contextname)
      caption.set_status(hildon.CAPTION_MANDATORY)
      vbox.pack_start(caption, False, False, 0)
    else:
      contextname = None
      self.wizard_state["context"] = contextname

    notebook.append_page(vbox, gtk.Label("Basics"))

    # Page 3
    vbox = gtk.VBox(False, 0)

    syncurl = hildon.Entry(gtk.HILDON_SIZE_AUTO)
    syncurl.set_input_mode(gtk.HILDON_GTK_INPUT_MODE_FULL | gtk.HILDON_GTK_INPUT_MODE_DICTIONARY)
    self.wizard_state["syncurl"] = syncurl
    caption = hildon.Caption(None, "Sync URL", syncurl)
    vbox.pack_start(caption, False, False, 0)

    username = hildon.Entry(gtk.HILDON_SIZE_AUTO)
    username.set_input_mode(gtk.HILDON_GTK_INPUT_MODE_FULL | gtk.HILDON_GTK_INPUT_MODE_DICTIONARY)
    self.wizard_state["user"] = username
    caption = hildon.Caption(None, "Account", username)
    vbox.pack_start(caption, False, False, 0)

    password = hildon.Entry(gtk.HILDON_SIZE_AUTO)
    password.set_input_mode(gtk.HILDON_GTK_INPUT_MODE_FULL | gtk.HILDON_GTK_INPUT_MODE_INVISIBLE)
#    password.set_visibility(False)
    self.wizard_state["pass"] = password
    caption = hildon.Caption(None, "Password", password)
    vbox.pack_start(caption, False, False, 0)

    notebook.append_page(vbox, gtk.Label("Server access"))

    # Page 4
    area = hildon.PannableArea()
    area.set_size_request_policy(hildon.SIZE_REQUEST_MINIMUM)
    vbox = gtk.VBox(False, 0)
    area.add_with_viewport(vbox)
    self.wizard_state["dbvbox"] = vbox
    self.wizard_state["dbarea"] = area

    label = gtk.Label("Database configuration is not currently available for this service type. SyncEvolution will use automatic settings.")
    label.set_line_wrap(True)
    self.wizard_state["dblabel"] = label
    # label will be connected to area on demand.

    notebook.append_page(area, gtk.Label("Server databases"))

    # Page 5
    vbox = gtk.VBox(False, 0)

    label = gtk.Label("Service setup is complete. Tap 'Finish' to save or discard the settings by tapping outside the wizard.")
    label.set_line_wrap(True)
    vbox.pack_start(label, False, False, 0)

    notebook.append_page(vbox, gtk.Label("Complete"))

    # Done

    dialog = hildon.WizardDialog(self.main_window, "Add new service", notebook)
    dialog.set_forward_page_func(self.server_create_page_func)
    notebook.connect("switch-page", self.server_create_page_switch, dialog)
    dialog.show_all()
    response = dialog.run()

    if response == hildon.WIZARD_DIALOG_FINISH:
      template_idx = selector.get_active(0)
      template, temp_name, temp_custom = templates[template_idx]
      user_name = username.get_text()
      user_pass = password.get_text()

      server_name = servername.get_text()
      if has_contexts:
        context = contextname.get_text()
        if context == "":
          context = server_name
        server = "%s@%s" % (server_name, context)
      else:
        server = server_name
        context = None

      temp_config = self.get_template_config(template)
      peer_type = self.get_peer_type(temp_config)
      temp_sources = self.get_template_sources(temp_config)

      std_config = {"printChanges": "0",
                    "SSLVerifyServer": "0"}
      config = std_config.copy()
      config["syncURL"] = syncurl.get_text()
      if temp_custom:
        config["WebURL"] = ""

      disabled_sources = []
      for name, source_url in self.wizard_state["sources"].items():
        config1 = {}
        uri = source_url.get_text()
        if uri == "":
          disabled_sources.append(name)
        else:
          config1["uri"] = uri
        if template == dav_template:
          # "Generic DAV" pseudo-template
          config2 = self.sync.get_source_config(dav_config, name)
          config1["backend"] = config2["backend"]
        self.sync.set_source_config(config, name, config1)

      if len(self.wizard_state["sources"]) == 0 and template == dav_template:
        config["sources"] = dav_config["sources"]

      if peer_type.startswith("SyncML"):
        # nice and simple SyncML configuration
        for name in disabled_sources:
          config1 = self.sync.get_source_config(config, name)
          config1["sync"] = "disabled"
        self.sync.create_server(server,
          template,
          user_name,
          user_pass,
          config)
      else:
        # non-SyncML configuration chosen, need to configure
        # two SyncML configurations talking to each other
        src_context = context + "-target"
        src_server = "target-config@" + src_context

        # First, configure "remote" side
        src_template = template
        src_config = config
        src_config["consumerReady"] = "0" # to hide config from GUIs
        if template == dav_template:
          # "Generic DAV" pseudo-template
          for name in dav_config.keys():
            if name == "sources":
              continue
            src_config[name] = dav_config[name]
          src_template = None

        self.sync.create_server(src_server,
          src_template,
          user_name,
          user_pass,
          src_config,
          temp_sources)

        # Now, configure "local" side
        config = std_config.copy()
        config["syncURL"] = "local://@" + src_context
        for key in ["WebURL", "PeerName", "consumerReady"]:
          if temp_config.has_key(key):
            config[key] = temp_config[key]
        for name in disabled_sources:
          config1 = {"sync": "disabled"}
          self.sync.set_source_config(config, name, config1)
        self.sync.create_server(server,
          "SyncEvolution Client",
          "",
          "",
          config,
          temp_sources)

      self.init_main_selector()

    dialog.destroy()
    self.wizard_state = None

  def server_create_page_switch(self, notebook, page, page_num, dialog):
    dialog.set_response_sensitive(hildon.WIZARD_DIALOG_FINISH, page_num >= 4)

  def server_create_get_template(self):
    selector = self.wizard_state["template"]
    templates = self.wizard_state["templates"]
    template_idx = selector.get_active(0)
    return templates[template_idx]
  def server_create_page_func(self, notebook, page_num, user_data):
    if page_num == 1:
      servername = self.wizard_state["name"]
      if len(servername.get_text()) != 0:
        template, temp_name, temp_custom = self.server_create_get_template()
        temp_config = self.get_template_config(template)

        syncurl = self.wizard_state["syncurl"]

        if temp_custom:
          syncurl.set_text("")
        else:
          syncurl.set_text(temp_config.get("syncURL", ""))

        return True
      else:
        return False

    if page_num == 2:
      syncurl = self.wizard_state["syncurl"]
      if len(syncurl.get_text()) != 0:
        template, temp_name, temp_custom = self.server_create_get_template()
        temp_config = self.get_template_config(template)
        peer_type = self.get_peer_type(temp_config)

        source_list = self.build_source_list(temp_config)

        vbox = self.wizard_state["dbvbox"]
        dbarea = self.wizard_state["dbarea"]
        dblabel = self.wizard_state["dblabel"]

        # avoid problems by hiding stuff while we mess around
        dbarea.hide_all()

        if self.wizard_state.has_key("sources"):
          # remove any previously created source fields
          sources_remote = self.wizard_state["sources"]
          for name, source_url in sources_remote.items():
            # the vbox children are the captions (source_url.parent)
            vbox.remove(source_url.parent)
          if len(sources_remote) == 0:
            vbox.remove(dblabel)

        # add source fields
        sources_remote = {}
        for name, label in source_list:
          config = self.sync.get_source_config(temp_config, name)
          if peer_type.startswith("SyncML"):
            source_url = hildon.Entry(gtk.HILDON_SIZE_AUTO)
            source_url.set_placeholder("Disabled")
            source_url.set_text(config.get("uri", ""))
            caption = hildon.Caption(None, label + " database", source_url)
            vbox.pack_start(caption, False, False, 0)
            sources_remote[name] = source_url
        self.wizard_state["sources"] = sources_remote

        if len(sources_remote) == 0:
          vbox.pack_start(dblabel, False, False, 0)

        dbarea.show_all()

        return True
      else:
        return False

    return True
  def get_template_config(self, template):
    if template == dav_template:
      # "Generic DAV" pseudo-template
      return dav_config
    return self.sync.get_template_config(template)
  def get_template_sources(self, config):
    return self.sync.get_sources_from_config(config, all=True)
  def get_peer_type(self, config):
    peer_type = config.get("peerType")
    if peer_type is not None:
      return peer_type
    peer_client = config.get("PeerIsClient", "0")
    if peer_client == "1":
      return "SyncMLClient"
    return "SyncMLServer"
  def sync_is_local(self, config):
    url = config.get("syncURL", "")
    if url.startswith("local://@"):
      return 1
    else:
      return 0

### SERVER WINDOW
  def get_server_title(self):
    return "Sync with " + self.server_name
  def get_source_label(self, source):
    return source_names.get(source, source)
  def build_source_list(self, config, all=False):
    sources = self.sync.get_sources_from_config(config, all=all)
    # build source list with labels for the UI
    source_list = []
    # add known sources
    for name, label in default_sources:
      if name in sources:
        source_list.append((name, label))
        sources.remove(name)
    # add unknown sources
    for name in sources:
      source_list.append((name, name))
    return source_list
  def init_server_config(self):
    self.server_window = hildon.StackableWindow()
    self.server_window.set_title(self.get_server_title())
    self.server_window.connect("destroy", self.destroy_server_config)
    self.program.add_window(self.server_window)

    area = hildon.PannableArea()
    area.set_size_request_policy(hildon.SIZE_REQUEST_CHILDREN)
    self.server_window.add(area)
    vbox = gtk.VBox(False, 0)
    area.add_with_viewport(vbox)

    button = hildon.Button(THUMB_SIZE, hildon.BUTTON_ARRANGEMENT_VERTICAL, "Synchronize!")
    button.connect("clicked", self.sync_clicked)
    vbox.pack_start(button, False, False, 0)

    if self.server_config.has_key("WebURL"):
      button = hildon.Button(FINGER_SIZE, hildon.BUTTON_ARRANGEMENT_VERTICAL, "Open Service Provider Website", self.server_config["WebURL"])
      button.connect("clicked", self.weburl_clicked)
      vbox.pack_start(button, False, False, 0)

    if GUI_VERBOSITY > 0:
      self.sync_mode = hildon.TouchSelector(text = True)
      for source_mode in default_modes:
        self.sync_mode.append_text(source_mode[2])
      self.sync_mode.set_active(0, 0)

      button = hildon.PickerButton(FINGER_SIZE, hildon.BUTTON_ARRANGEMENT_HORIZONTAL)
      button.set_title("Synchronization Mode")
      button.set_selector(self.sync_mode)
      vbox.pack_start(button, False, False, 0)

    source_list = self.build_source_list(self.server_config)

    if GUI_VERBOSITY > 1:
      # create checkboxes for them
      self.sync_sources = []
      for name, label in source_list:
        button = hildon.CheckButton(FINGER_SIZE)
        button.set_label("Synchronize " + label)
        button.set_active(True)
        self.sync_sources.append((name, button))
        vbox.pack_start(button, False, False, 0)

    view_button = hildon.Button(FINGER_SIZE, hildon.BUTTON_ARRANGEMENT_VERTICAL, "View Synchronization History")
    view_button.connect("clicked", self.history_clicked)
    vbox.pack_start(view_button, False, False, 0)

    auto_button = hildon.CheckButton(FINGER_SIZE)
    auto_button.set_label("Automatically synchronize daily")
    auto_button.set_active(not self.server_cookie is None)
    vbox.pack_start(auto_button, False, False, 0)

    time_button = hildon.TimeButton(FINGER_SIZE, hildon.BUTTON_ARRANGEMENT_HORIZONTAL)
    time_button.set_title("Daily synchronization time")
    time_button.set_alignment(0.25, 0.5, 0.5, 0.5)
    vbox.pack_start(time_button, False, False, 0)

    self.load_sync_time(time_button)
    auto_button.connect("toggled", self.auto_sync_toggled, time_button)
    time_button.connect("value-changed", self.sync_time_changed)

    menu = hildon.AppMenu()
    button = hildon.GtkButton(gtk.HILDON_SIZE_AUTO)
    button.set_label("Edit service")
    button.connect("clicked", self.edit_server)
    menu.append(button)
    button = hildon.GtkButton(gtk.HILDON_SIZE_AUTO)
    button.set_label("Delete service")
    button.connect("clicked", self.delete_server)
    menu.append(button)
    menu.show_all()
    self.server_window.set_app_menu(menu)

    self.server_window.show_all()
  def destroy_server_config(self, window):
    self.server_window = None
    self.sync_sources = None
    self.sync_mode = None
  def edit_server(self, button):
    self.init_server_edit()
  def delete_server(self, button):
    note = hildon.hildon_note_new_confirmation(self.server_window, "Delete service %s?" % self.server_name)
    response = gtk.Dialog.run(note)
    note.destroy()
    if response == gtk.RESPONSE_OK:
      self.do_delete_server(self.server)
      self.server_window.destroy()
      self.init_main_selector()
  def sync_clicked(self, button):
    self.synchronize_start()
  def weburl_clicked(self, button):
    url = self.server_config["WebURL"]
    self.launch_browser(url)
  def history_clicked(self, button):
    self.init_server_history()
  def load_sync_time(self, time_button):
    if not self.server_cookie is None:
      event = alarm.get_event(self.server_cookie)
      recur = event.get_recurrence(0)
      hours = 0
      mask = recur.mask_hour
      while mask > 1:
        hours = hours + 1
        mask = mask >> 1
      minutes = 0
      mask = recur.mask_min
      while mask > 1:
        minutes = minutes + 1
        mask = mask >> 1
      time_button.set_time(hours, minutes)
  def auto_sync_toggled(self, auto_button, time_button):
    active = auto_button.get_active()
    if active:
      (hours, minutes) = time_button.get_time()
      event = alarm.Event()
      event.appid = "syncevolution"
      event.title = "Synchronization with " + self.server
#      event.flags |= alarm.EVENT_RUN_DELAYED
      action = event.add_actions(1)[0]
      action.flags |= alarm.ACTION_WHEN_TRIGGERED | alarm.ACTION_WHEN_DELAYED | alarm.ACTION_TYPE_EXEC
      action.command = os.path.abspath(sys.argv[0]) + " --quiet " + self.server
      recur = event.add_recurrences(1)[0]
      # let's see what this does...
      recur.mask_min = 1 << minutes
      recur.mask_hour = 1 << hours
      # initialize alarm time to somewhere in the future
      event.alarm_time = time.time() + 5
#      lt = time.localtime(time.time() + 5)
#      tz = time.tzname[lt.tm_isdst]
#      event.alarm_time = time.mktime(recur.next(lt, tz))
      event.recurrences_left = -1
      f = open(self.server_dir + "/.alarmcookie", "w")
      self.server_cookie = alarm.add_event(event)
      f.write("%d" % self.server_cookie)
      f.close()
    else:
      if not self.server_cookie is None:
        os.unlink(self.server_dir + "/.alarmcookie")
        alarm.delete_event(self.server_cookie)
        self.server_cookie = None
  def sync_time_changed(self, time_button):
    if not self.server_cookie is None:
      (hours, minutes) = time_button.get_time()
      event = alarm.get_event(self.server_cookie)
      f = open(self.server_dir + "/.alarmcookie", "w")
      recur = event.get_recurrence(0)
      recur.mask_min = 1 << minutes
      recur.mask_hour = 1 << hours
      self.server_cookie = alarm.update_event(event)
      f.write("%d" % self.server_cookie)
      f.close()
  def launch_browser(self, url):
    proxy_obj = self.dbus.get_object('com.nokia.osso_browser', '/com/nokia/osso_browser')
    dbus_iface = dbus.Interface(proxy_obj, 'com.nokia.osso_browser')
    dbus_iface.open_new_window(url)

### SERVER HISTORY
  def get_history_title(self):
    return "History for " + self.server
  def init_server_history(self):
    self.history_window = hildon.StackableWindow()
    self.history_window.set_title(self.get_history_title())
    self.history_window.connect("destroy", self.destroy_server_history)
    self.program.add_window(self.history_window)

    self.server_sessions = self.sync.get_sessions(self.server)
    self.server_sessions.reverse() # show newest first

    selector = hildon.TouchSelector(text = True)
    selector.set_hildon_ui_mode("normal")
    selector.connect("changed", self.server_history_selected)
    self.history_window.add(selector)

    # FIXME: left align the selector entries (can't figure out how)

    for (start_time, code, logf) in self.server_sessions:
      ts = time.strftime(TIMEFMT, time.localtime(start_time))
      if code is None:
        rs = "Aborted"
      elif code == 200:
        rs = "Success"
      else:
        rs = "Error %d" % code
      selector.append_text("%s [%s]" % (ts, rs))

    self.history_window.show_all()

  def server_history_selected(self, selector, column):
    row = selector.get_last_activated_row(column)[0]
    (start_time, code, logf) = self.server_sessions[row]
    if logf.endswith("syncevolution-log.html"):
      # For SyncEvolution 1.0+, skip to H2 anchor instead of the
      # H1 anchor used below, to skip timezone stuff.
      url = "file://" + logf + "#H2"
    else:
      # The first HTML anchor I found in the logs is named H1.
      # Although it's not the first line of the interesting part of the log,
      # using it might help the user avoid scrolling past a lot of config cruft.
      url = "file://" + logf + "#H1"
    self.launch_browser(url)

  def destroy_server_history(self, window):
    self.history_window = None
    self.server_sessions = None

### SERVER EDITING
  def init_server_edit(self):
    loc_url = self.server_config.get("syncURL", "")
    if loc_url.startswith("local://@"):
      is_local = 1
      src_server = "target-config@" + loc_url[9:]
      src_config = self.sync.get_server_config(src_server)
    else:
      is_local = 0
      src_server = self.server
      src_config = self.server_config

    peer_type = self.get_peer_type(src_config)

    dialog = gtk.Dialog()
    dialog.set_transient_for(self.server_window)
    dialog.set_title("Edit " + self.server_name)
    dialog.add_button("Done", gtk.RESPONSE_OK)

    area = hildon.PannableArea()
    area.set_size_request_policy(hildon.SIZE_REQUEST_CHILDREN)
    vbox = gtk.VBox(False, 0)
    area.add_with_viewport(vbox)
    dialog.vbox.add(area)

    sync_url = hildon.Entry(gtk.HILDON_SIZE_AUTO)
    sync_url.set_text(src_config.get("syncURL", ""))
    caption = hildon.Caption(None, "Sync URL", sync_url)
    vbox.pack_start(caption, False, False, 0)

    web_url = hildon.Entry(gtk.HILDON_SIZE_AUTO)
    web_url.set_text(self.server_config.get("WebURL", ""))
    caption = hildon.Caption(None, "Web URL", web_url)
    vbox.pack_start(caption, False, False, 0)

    username = hildon.Entry(gtk.HILDON_SIZE_AUTO)
    username.set_text(src_config.get("username", ""))
    caption = hildon.Caption(None, "Username", username)
    vbox.pack_start(caption, False, False, 0)

    password = hildon.Entry(gtk.HILDON_SIZE_AUTO)
    password.set_text(src_config.get("password", ""))
    password.set_visibility(False)
    caption = hildon.Caption(None, "Password", password)
    vbox.pack_start(caption, False, False, 0)

    source_list = self.build_source_list(src_config, True)

    # load configs
    sources_srv_config = {}
    sources_src_config = {}
    sources_virtual = {}
    sources_combined = {}
    for name, label in source_list:
      config1 = self.sync.get_source_config(self.server_config, name)
      config2 = self.sync.get_source_config(src_config, name)
      sources_srv_config[name] = config1
      sources_src_config[name] = config2
      # SyncEvolution 1.2 config
      type = config1.get("backend")
      base = config1.get("database")
      if type is None:
        # SyncEvolution 1.0 config
        type = config1.get("type")
        base = config1.get("evolutionsource")
        if type is not None:
          epos = type.find(":")
          if epos >= 0:
            type = type[:epos]
      # check for virtual source
      if type == "virtual" or \
         (type is None and base is not None and base.find(",") != -1):
        if base is not None:
          vsources = base.split(",")
          sources_virtual[name] = vsources
          for vsource in vsources:
            sources_combined[vsource] = (name, label)
        else:
          sources_virtual[name] = []

    # create intuitive ordering of sources
    source_order = []
    for nsource in source_list:
      if nsource in source_order:
        continue
      if sources_combined.has_key(nsource[0]):
        vsource = sources_combined[nsource[0]]
        if not vsource in source_order:
          source_order.append(vsource)
      source_order.append(nsource)
    source_list = source_order

    # server-specific source configuration
    sources_remote = {}
    for name, label in source_list:
      if not sources_combined.has_key(name):
        config2 = sources_src_config[name]
        if not is_local:
          source_url = hildon.Entry(gtk.HILDON_SIZE_AUTO)
          source_url.set_text(config2.get("uri", ""))
          caption = hildon.Caption(None, label + " database", source_url)
          vbox.pack_start(caption, False, False, 0)
          sources_remote[name] = source_url
        else:
          source_url = hildon.Entry(gtk.HILDON_SIZE_AUTO)
          source_url.set_text(config2.get("database", ""))
          caption = hildon.Caption(None, label + " database", source_url)
          vbox.pack_start(caption, False, False, 0)
          sources_remote[name] = source_url

    # client-specific source configuration
    backends = self.sync.get_backends()
    sources_mode = {}
    sources_base = {}
    for name, label in source_list:
      config1 = sources_srv_config[name]
      config2 = sources_src_config[name]

      if not sources_combined.has_key(name):
        mode_selector = hildon.TouchSelector(text = True)
        mode = config1.get("sync", "normal")
        idx = 0
        count = 0
        for source_mode in source_modes:
          if mode == source_mode[is_local]:
            idx = count
          mode_selector.append_text(source_mode[2])
          count += 1
        mode_selector.set_active(0, idx)

        button = hildon.PickerButton(FINGER_SIZE, hildon.BUTTON_ARRANGEMENT_VERTICAL)
        button.set_alignment(0, 0.5, 0.5, 0.5)
        button.set_title(label + " synchronization")
        button.set_selector(mode_selector)
        vbox.pack_start(button, False, False, 0)
        sources_mode[name] = mode_selector

      if not sources_virtual.has_key(name):
        base_selector = hildon.TouchSelector(text = True)
        base_list = []
        base = config1.get("database") # SyncEvolution 1.2
        if base is None:
          base = config1.get("evolutionsource") # SyncEvolution 1.0
        idx = 0
        count = 0
        for backend_id, bases in backends.items():
          backend = backend_aliases.get(backend_id, backend_id)
          if backend != name:
            continue
          for base_name, base_uri, default in bases:
            if base is None:
              if default:
                idx = count
                base_uri = None # if the value was unset, leave unset if the user keeps the setting...
            elif base == base_uri:
              idx = count
            base_list.append((backend, base_uri))
            base_selector.append_text(calendar_names.get(base_name, base_name))
            count += 1
        base_selector.set_active(0, idx)

        # there's only one contacts database, so we don't have to show a button for that
        if count > 1:
          button = hildon.PickerButton(FINGER_SIZE, hildon.BUTTON_ARRANGEMENT_VERTICAL)
          button.set_alignment(0, 0.5, 0.5, 0.5)
          button.set_title(label + " source")
          button.set_selector(base_selector)
          vbox.pack_start(button, False, False, 0)

        sources_base[name] = (base_selector, base_list)

    dialog.show_all()
    response = dialog.run()
    if response == gtk.RESPONSE_OK:
      config = {}
      config["syncURL"] = sync_url.get_text()
      config["WebURL"] = web_url.get_text()
      config["username"] = username.get_text()
      config["password"] = password.get_text()
      for name, label in source_list:
        source_config = {}
        if not sources_combined.has_key(name) and sources_remote.has_key(name):
          source_url = sources_remote[name]
          if not is_local:
            source_config["uri"] = source_url.get_text()
          else:
            source_config["database"] = source_url.get_text()
        self.sync.set_source_config(config, name, source_config)
      if is_local:
        self.sync.configure_server(src_server, config)
        config = {}
      # set WebURL in both configs, for good measure
      config["WebURL"] = web_url.get_text()
      for name, label in source_list:
        source_config = {}
        if not sources_combined.has_key(name):
          mode_selector = sources_mode[name]
          idx = mode_selector.get_active(0)
          source_config["sync"] = source_modes[idx][is_local]
        if not sources_virtual.has_key(name):
          base_selector, base_list = sources_base[name]
          idx = base_selector.get_active(0)
          # SyncEvolution 1.2 renamed the property "evolutionsource"
          # to "database" (thus we needed to check for both above).
          # However, we can still configure using just the old name,
          # and SyncEvolution will transparently map it to the new name.
          # That way we don't need version checks here...
          if idx >= 0:
            source_config["evolutionsource"] = base_list[idx][1]
        self.sync.set_source_config(config, name, source_config)
      self.sync.configure_server(self.server, config)

    dialog.destroy()

    if response == gtk.RESPONSE_OK:
      self.server_config = self.sync.get_server_config(self.server)
      # Rather than trying to update the existing server window
      # to match the new configuration, just recreate it from scratch,
      # at least for now
      self.server_window.destroy()
      self.init_server_config()

### SYNCHRONIZATION PROGRESS
  def synchronize_start(self, mode = None):
    # get parameters
    is_local = self.sync_is_local(self.server_config)
    if mode is None and not self.sync_mode is None:
      idx = self.sync_mode.get_active(0)
      mode = default_modes[idx][is_local]
    sources = None
    if not self.sync_sources is None:
      sources = []
      for name, button in self.sync_sources:
        if button.get_active():
          sources.append(name)
      if len(sources) == 0:
        banner = hildon.hildon_banner_show_information(self.server_window, "", "Nothing to synchronize")
        return

    # build progress dialog
    self.sync_progress = None
    self.sync_bar = gtk.ProgressBar()
    self.sync_bar.set_text("Synchronizing...")

    self.sync_dialog = hildon.hildon_note_new_cancel_with_progress_bar(self.server_window, "Synchronizing with " + self.server_name, self.sync_bar)
    self.sync_dialog.connect("response", self.synchronize_cancel)
    # I don't seem able to trigger "close" or "delete_event", but just in case anyone can...
    self.sync_dialog.connect("close", self.synchronize_close)
    self.sync_dialog.connect("delete_event", self.synchronize_quit)

    self.sync_dialog.show_all()

    # start synchronization
    self.sync_state = SyncRunner(False, self.sync, self.server, self.server_dir, self.server_config, mode, sources)
    self.sync_state.set_callbacks(self.synchronize_poll, self.synchronize_tick)

  def synchronize_cleanup(self):
    if self.sync_dialog is None:
      return
    self.sync_dialog.destroy()
    self.sync_bar = None
    self.sync_progress = None
    self.sync_dialog = None
    status = self.sync_state.finish()
    self.sync_state = None
    if status["result"] == 0:
      banner = hildon.hildon_banner_show_information(self.server_window, "", "Synchronization successful")
    elif status["result"] == RESULT_LOCKED:
      banner = hildon.hildon_banner_show_information(self.server_window, "", "Synchronization already in progress")
    elif status["result"] == RESULT_ABORTED:
      banner = hildon.hildon_banner_show_information(self.server_window, "", "Synchronization aborted by user")
    elif status["result"] == RESULT_NO_CONNECTION:
      note = hildon.hildon_note_new_information(self.server_window, "Synchronization failed: no network connection")
      response = gtk.Dialog.run(note)
    elif status.has_key("status") and status["status"] == 22000:
      self.synchronize_recover("Select first-time sync mode")
    else:
      msg = "Synchronization failed"
      if status.has_key("error"):
        msg += ": " + status["error"]
      note = hildon.hildon_note_new_information(self.server_window, msg)
      response = gtk.Dialog.run(note)

  def synchronize_cancel(self, dialog, id):
    self.sync_state.abort()

  def synchronize_close(self, dialog):
    self.sync_state.abort()

  def synchronize_quit(self, dialog, event):
    self.sync_state.abort()
    return True

  def synchronize_poll(self, source, condition):
    if self.sync_state is None:
      return False
    if self.sync_state.poll():
      self.synchronize_cleanup()
      return False
    else:
      self.sync_progress = self.sync_state.progress()
      if not self.sync_progress is None:
        self.sync_bar.set_fraction(self.sync_progress)
      return True
  def synchronize_tick(self):
    if self.sync_progress is None:
      self.sync_bar.pulse()
    return True

  def synchronize_recover(self, title):
    is_local = self.sync_is_local(self.server_config)
    mode_selector = hildon.TouchSelector(text = True)
    for source_mode in default_modes[1:4]:
      mode_selector.append_text(source_mode[2])
    dialog = hildon.PickerDialog(self.server_window)
    dialog.set_title(title)
    dialog.set_selector(mode_selector)
    dialog.show_all()
    response = dialog.run()
    mode = None
    if response == gtk.RESPONSE_OK:
      idx = mode_selector.get_active(0)
      mode = default_modes[idx+1][is_local]
    dialog.destroy()
    if not mode is None:
      self.synchronize_start(mode)

class SyncCLI(object):
  def __init__(self, server, quiet=False):
    gtk.set_application_name("SyncEvolution")
    self.sync = syncevolution.SyncEvolution(quiet)
    self.server = server
    self.server_dir = self.sync.get_server_dir(self.server)
    self.server_config = self.sync.get_server_config(self.server)
    self.osso_ctx = osso.Context("SyncEvolution", "1.0")
    self.osso_note = osso.SystemNote(self.osso_ctx)
  def synchronize(self):
    self.sync_state = SyncRunner(True, self.sync, self.server, self.server_dir, self.server_config)
    self.sync_state.set_callbacks(self.synchronize_poll)
    if self.sync_state is None:
      return
    self.old_sigint = signal.signal(signal.SIGINT, self.synchronize_cancel)
    gtk.main()
    signal.signal(signal.SIGINT, self.old_sigint)
  def synchronize_cleanup(self):
    status = self.sync_state.finish()
    self.sync_state = None
    if status["result"] == 0:
      self.osso_note.system_note_infoprint("Synchronization successful")
    elif status["result"] == RESULT_LOCKED:
      # if another synchronization is in progress anyway, this is probably ok to ignore
      pass
    elif status["result"] == RESULT_ABORTED:
      self.osso_note.system_note_infoprint("Synchronization aborted by user")
    elif status["result"] == RESULT_NO_CONNECTION:
#      self.osso_note.system_note_infoprint("Synchronization failed: No connection")
      self.osso_note.system_note_dialog("Synchronization failed: No connection", 'notice')
    else:
      msg = "Synchronization failed"
      if status.has_key("error"):
        msg += ": " + status["error"]
      self.osso_note.system_note_dialog(msg, 'error')
    if gtk.main_level() > 0:
      gtk.main_quit()
  def synchronize_cancel(self, sig, frame):
    self.sync_state.abort()
  def synchronize_poll(self, source, condition):
    if self.sync_state.poll():
      self.synchronize_cleanup()
      return False
    else:
      return True
