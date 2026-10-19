## Running repros

You can execute code in exactly one way: write a repro file to your scratch dir with the Write
tool, then run it at head and base with this command (copy it exactly, changing only the file
name; nothing else is allowed in the shell, and every other command is refused):

```
$replay_cmd
```

- Files named `test_*.py` run under pytest from the repo's tests dir (so the repo's fixtures and
  conftest apply); other names run as scripts with `python`.
- The repo's code is importable as it is in the test suite. The network is off, the tree is
  read-only, and only `/tmp` is writable. Each side is limited to $replay_timeout seconds.
- Add `--differential` to compare the output of the same script at head and base.
- The command prints JSON with each side's outcome (`pass`, `failed`, `error`, `timeout`), exit
  code, failure signature and the tail of the output. A repro proves a defect when it fails at
  head as you claim and passes at base (or, for differential repros, when the outputs differ in
  the way you claim). `error` means the repro itself is broken: fix it and run it again.
