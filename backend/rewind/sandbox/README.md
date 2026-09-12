# Sandbox backends

One contract (`Sandbox` in `__init__.py`), three backends with identical observable behaviour. `create_sandbox(spec, backend)`
picks one: `REWIND_SANDBOX=local|docker|daytona`, or `auto` = daytona if `DAYTONA_API_KEY` is set, else docker if `docker` is on
PATH, else local.

| Contract step | `local.py` | `docker.py` | `daytona.py` |
|---|---|---|---|
| copy tree in | `shutil.copytree` (ignores `SKIP_DIRS`) | `tar_tree()` streamed to `docker cp - <c>:/work` | `tar_tree(compress=True)` via `sandbox.fs.upload_file`, then `tar -xzf` inside |
| repo root | `.rewind/sandboxes/<id>` | `/work` | `<sandbox.get_work_dir()>/rewind` (or `DAYTONA_WORKDIR`) |
| git baseline | `git init` / `add -A` / `commit` with `GIT_ENV` | same, via `docker exec git -c user.name=rewind -c user.email=rewind@local ...` | same, via `sandbox.process.exec` |
| `.git/info/exclude` | written directly | `write_file()` | `write_file()` |
| install deps | `spec.install_cmds` unless `venv_python` given | `spec.install_cmds` through `run()`; `REWIND_PIP_CACHE` mounted at `/root/.cache/pip` | `spec.install_cmds` through `run()` |
| `list_files` | `os.walk`, dirs first then files, sorted, `SKIP_DIRS`/`*.egg-info` dirs and `*.pyc` skipped, `... truncated at N entries` | one `find` (pruning `SKIP_DIRS`) then `walk_from_find()` rebuilds the exact `os.walk` order | same `find` + `walk_from_find()` |
| `read_file` | `read_text(utf-8, replace)` | `docker exec cat`, bytes decoded utf-8/replace | `cat` through exec |
| `write_file` | `write_text(utf-8, newline="\n")`, parents created | host temp file (utf-8, LF) -> `mkdir -p` + `docker cp` | `mkdir -p` + `fs.upload_file(bytes)` |
| `run()` | `python` -> venv python; `PYTHONPATH=root[:root/src]`; `subprocess` timeout | `python` -> `REWIND_DOCKER_PYTHON` (default `python`); `sh -c '<PYTHONPATH>; exec timeout -k 5 N "$@"'`; rc 124 -> `timed_out` | `python` -> `DAYTONA_PYTHON`; `bash -c` with the same `timeout`, stdout/stderr captured to files and echoed back with markers (the SDK only returns one merged stream); rc 124 -> `timed_out` |
| `diff` / `changed_files` | `git add -A; git diff --cached ...` | same, inside the container | same, inside the sandbox |
| `close()` | `rmtree` | `docker rm -f` | `sandbox.delete()` |

All three set `PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 PY_COLORS=0`, return `returncode=-1` plus `[timed out after Ns]` in stderr on a
timeout, and raise `ValueError` for paths that escape the repo root and `FileNotFoundError` for missing ones.

## Docker

* `REWIND_DOCKER_IMAGE` (default `python:3.11-slim`). Slim images have no git; `create()` runs `apt-get install git` once per container
  when `git` is missing (~20 s). Point at an image with git + pytest preinstalled to skip that.
* Container: `docker run -d --name rewind-<id> [-v $REWIND_PIP_CACHE:/root/.cache/pip] -w /work <image> sleep infinity`.
* Every call is a `docker` CLI invocation with bytes in/out, so utf-8 and LF survive a Windows host.
* Tests: `tests/test_docker_sandbox.py` monkeypatches `subprocess.run`, asserts each method's exact argv, and checks `list_files`
  against `LocalSandbox` using real `find` output from Git Bash.

## Daytona

* SDK: `pip install daytona` (verified against 0.211.2; API names cited in the module docstring).
* Env: `DAYTONA_API_KEY` (required), `DAYTONA_API_URL`, `DAYTONA_TARGET`; `DAYTONA_SNAPSHOT` -> `CreateSandboxFromSnapshotParams`,
  else `DAYTONA_IMAGE` -> `CreateSandboxFromImageParams`, else Daytona's default python snapshot. Sandboxes carry the label
  `rewind=rewind-<id>`.
* Tests: `tests/test_daytona_sandbox.py` fakes the client with the same attribute paths (`create`, `get_work_dir`, `fs.upload_file`,
  `process.exec`, `delete`), records the call sequence, and executes the commands for real through Git Bash so listing, read/write,
  diff and timeouts are compared with `LocalSandbox` on the same tree.

Shared helpers (`tar_tree`, `find_command`, `walk_from_find`, `safe_path`, `run_script`) live in `docker.py` and are imported by
`daytona.py`; importing `docker.py` needs no docker binary.
