#!/usr/bin/python2
import subprocess, fcntl, os, signal, shutil

def set_nonblock(f):
  fd = f.fileno()
  fl = fcntl.fcntl(fd, fcntl.F_GETFL)
  fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

def parse_config(data):
  master = {}
  current = master
  for line in data.splitlines():
    if line.startswith("#"):
      continue
    if line.startswith("["):
      pos = line.find("]")
      if pos < 0:
        continue
      key = master.setdefault("sources", {})
      value = line[1:pos]
      current = key.setdefault(value, {})
      continue
    pos = line.find(" = ")
    if pos < 0:
      continue
    key = line[:pos].strip()
    value = line[pos+3:].strip()
    current[key] = value
  return master

# Whether to include the "preparing" stuff in the progress bar fraction.
# Since some servers are real slow, it may be better to keep the pulsing progress bar
# and only start filling it once the server starts sending data back to us.
include_preflight = 0

class SyncState(object):
  def __init__(self, proc, server, quiet):
    st = server.split("@", 1)
    if len(st) < 2:
      st.append("default")
    self.proc = proc
    self.server = st[0]
    self.context = st[1]
    self.quiet = quiet
    self.sources = []
    self.data = ""
    self.parse = ""
    self.stages = 0
    self.stage = None
    self.finish = False
    self.errlog = None
  def parse_line(self, line):
    if line.startswith("Synchronization successful") or \
       line.startswith("Synchronization failed"):
      self.stage = self.stages
      self.finish = True
      if line.startswith("Synchronization failed, see "):
        pos = line.find(" for details")
        self.errlog = line[28:pos]
      return
    if line.startswith("[INFO] ") and not self.finish:
      data = line[7:]
      pos = data.find(": ")
      if pos < 0:
        return
      source = data[:pos]
      state = data[pos+2:]

      if source[:1] == "@":
        context, source = source[1:].split("/", 1)
      else:
        context = "default"

      if context == self.context:
        if not source in self.sources:
          if state.startswith("starting"):
            self.sources.append(source)
            if include_preflight:
              self.stages = len(self.sources) * 2
            else:
              self.stages = len(self.sources)
          else:
            return
        source_idx = self.sources.index(source)
        if state.startswith("preparing"):
          if include_preflight:
            self.stage = source_idx
        elif state.startswith("started"):
          if include_preflight:
            self.stage = len(self.sources) + source_idx
          else:
            self.stage = source_idx
  def watches(self):
    if self.proc is None:
      return []
    return [self.proc.stdout]
  def poll(self):
    if self.proc is None:
      return True
    i = self.proc.stdout.read()
    self.data += i
    self.parse += i
    while True:
      pos = self.parse.find("\n")
      if pos < 0:
        break
      line = self.parse[:pos]
      self.parse = self.parse[pos+1:]
      if not self.quiet:
        print line
      self.parse_line(line)
    if not self.proc.poll() is None:
      return True
    return False
  def progress(self):
    if self.stage is None:
      return None
    if self.stages == 0:
      return None
    return float(self.stage) / self.stages
  def complete(self):
    if self.proc is None:
      return True
    return not self.proc.returncode is None
  def result(self):
    if self.proc is None:
      return -1
    return self.proc.returncode
  def abort(self):
    if self.proc is None:
      return
    os.kill(self.proc.pid, signal.SIGINT)

class SyncEvolution(object):
  def __init__(self, quiet=False):
    self.path = "syncevolution"
    self.quiet = quiet
  def launch(self, args):
#    print "syncevolution", " ".join(args)
    return subprocess.Popen([self.path] + args, stdout=subprocess.PIPE)
  def get_version(self):
    p = self.launch(["--version"])
    data = p.communicate()[0]
    lines = data.splitlines()
    ltoks = lines[0].split(" ")
    return ltoks[1]
  def has_version(self, ver):
    v = self.get_version()
    stoks = v.split(".")
    dtoks = ver.split(".")
    return stoks >= dtoks
  def get_servers(self):
    p = self.launch(["--print-servers"])
    data = p.communicate()[0]
    return parse_config(data).keys()
  def get_server_dir(self, server):
    p = self.launch(["--print-servers"])
    data = p.communicate()[0]
    return parse_config(data).get(server)
  def get_server_config(self, server):
    p = self.launch(["--print-config", "--quiet", server])
    data = p.communicate()[0]
    return parse_config(data)
  def get_sources_from_config(self, server_config, sources=None, all=False):
    source_config = server_config.get("sources")
    ret = []
    if source_config is None:
      return ret
    for source, config in source_config.items():
      sync = config.get("sync")
      if not all and (sync is None or sync == "disabled" or sync == "none"):
        continue
      if sources is None or source in sources:
        ret.append(source)
    ret.sort()
    return ret
  def get_source_config(self, server_config, source):
    source_config = server_config.get("sources")
    if source_config is None:
      return None
    return source_config.get(source)
  def set_source_config(self, server_config, source, config):
    source_config = server_config.setdefault("sources", {})
    source_config[source] = config
  def get_templates(self):
    p = self.launch(["--template", "?"])
    data = p.communicate()[0]
    return parse_config(data).keys()
  def get_template_config(self, template):
    p = self.launch(["--print-config", "--quiet", "--template", template])
    data = p.communicate()[0]
    return parse_config(data)
  def has_contexts(self):
    return self.has_version("1.0")
  def get_session_dirs(self, server):
    p = self.launch(["--print-sessions", "--quiet", server])
    data = p.communicate()[0]
    if data == "":
      return []
    return data.splitlines()
  def get_sessions(self, server):
    sessions = []
    for dir in self.get_session_dirs(server):
      status_fn = os.path.join(dir, "status.ini")
      try:
        info_data = open(status_fn, "r").read()
      except:
        continue
      log_fn = os.path.join(dir, "syncevolution-log.html")
      if not os.access(log_fn, os.F_OK):
        log_fn2 = os.path.join(dir, "sysynclib_linux.html")
        if os.access(log_fn2, os.F_OK):
          log_fn = log_fn2
      info = parse_config(info_data)
      if info.has_key("start"):
        start_time = int(info["start"].split(",")[0])
      else:
        continue
      if info.has_key("status"):
        code = int(info["status"])
      else:
        code = None
      sessions.append((start_time, code, log_fn))
    return sessions

  def get_backends(self):
    p = self.launch([])
    data = p.communicate()[0]
    ret = {}
    key = None
    current = None
    for line in data.splitlines():
      if line == "":
        pass
      elif line.startswith(" "):
        if not line.startswith("   "):
          # source listing apparently complete
          del ret[key]
          break
        epos = line.rfind(")")
        spos = line.rfind(" (")
        if spos < 0 or epos < 0:
          break
        name = line[:spos].strip()
        uri = line[spos+2:epos]
        default = line[epos+1:].startswith(" <default>")
        current.append((name, uri, default))
      else:
        spos = line.rfind(" = ")
        if spos < 0:
          epos = line.find(":")
          key = line[:epos]
        else:
          epos = line.find(":", spos)
          key = line[spos+3:epos]
        current = ret.setdefault(key, [])
    return ret

  def create_server(self, server, template, username=None, password=None, config=None, sources=None):
    args = ["--configure"]
    if not template is None:
      args.extend(["--template", template])
    if not username is None:
      args.extend(["--sync-property", "username=" + username])
    if not password is None:
      args.extend(["--sync-property", "password=" + password])
    source_config = None
    if not config is None:
      for key, value in config.items():
        if key == "sources":
          source_config = value
          continue
        args.extend(["--sync-property", key + "=" + value])
    args.append(server)
    # note that without a template, we need another step.
    if not sources is None and not template is None:
      args.extend(sources)
    p = self.launch(args)
    data = p.communicate()[0]
    if not source_config is None:
      self.configure_server(server, {"sources": source_config})
    if not sources is None and template is None:
      # second step of templateless configuration
      args = ["--configure"]
      args.append(server)
      args.extend(sources)
      p = self.launch(args)
      data = p.communicate()[0]

  def delete_server(self, server):
    # syncevolution 0.9 can't do this on its own, delete the directory ourselves
    # delete session caches
    for dir in self.get_session_dirs(server):
      try:
        shutil.rmtree(dir)
      except:
        return False
    # delete configuration
    dir = self.get_server_dir(server)
    if dir is None:
      return False
    try:
      shutil.rmtree(dir)
      return True
    except:
      return False
  def delete_context(self, context):
    # For contexts, there's no direct way to determine which path
    # to rmtree, so leavee the destruction to syncevolution
    # (contexts are a syncevolution 1.0 feature anyway)
    p = self.launch(["--remove", "@" + context])
    data = p.communicate()[0]
  def configure_server(self, server, server_config):
    source_config = None
    args = ["--configure"]
    count = 0
    for key, value in server_config.items():
      if key == "sources":
        source_config = value
        continue
      args.extend(["--sync-property", key + "=" + value])
      count += 1
    args.append(server)
    if count > 0:
      p = self.launch(args)
      data = p.communicate()[0]
    if not source_config is None:
      for source, config in source_config.items():
        args = ["--configure"]
        count = 0
        for key, value in config.items():
          if value is None:
            value = ""
          args.extend(["--source-property", key + "=" + value])
          count += 1
        args.append(server)
        args.append(source)
        if count > 0:
          p = self.launch(args)
          data = p.communicate()[0]

  def synchronize_start(self, server, config, mode=None, sources=None, proxy=None):
    args = ["--run"]
    if not mode is None:
      args.extend(["--sync", mode])
    if not proxy is None:
      args.extend(["--sync-property", "useProxy=T", "--sync-property", "proxyHost=" + proxy])
    args.append(server)
    if not sources is None:
      if len(sources) == 0:
        return SyncState(None)
      args.extend(sources)
    if not self.quiet:
      print self.path, " ".join(args)
    p = self.launch(args)
    set_nonblock(p.stdout)
    return SyncState(p, server, self.quiet)
  def synchronize_status(self, state):
    ret = {"result": state.result()}
    if state.errlog is not None:
      logdir = os.path.dirname(state.errlog)
      statusfile = os.path.join(logdir, "status.ini")
      status = parse_config(open(statusfile, "r").read())
      code = int(status["status"])
      if status.has_key("error"):
        msg = status["error"]
      else:
        msg = None
      pos = msg.find(" (")
      if pos >= 0:
        # to keep the message short enough to fit on display (and not confuse user),
        # this removes unnecessary technical detail from message...
        msg = msg[:pos]
      ret["status"] = code
      ret["error"] = msg
    return ret
