#!/usr/bin/python2
import sys
import syncfe

quiet = False
args = sys.argv[1:]
if len(args) > 0 and args[0] == "--quiet":
  args.pop(0)
  quiet = True
if len(args) > 0:
  syncfe.SyncCLI(args[0], quiet).synchronize()
else:
  syncfe.SyncGUI(quiet).main()
