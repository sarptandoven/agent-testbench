# The docker backend

Plans and live sessions in a fresh Docker container on this machine: a new container for every run, optionally
with no network at all, the project as a copy, and (with NVIDIA's Container Toolkit) the machine's GPUs. It is the
sandbox for code nobody has reviewed yet, and the way to use a GPU box's drivers without installing anything on it.

```yaml
docker:
  image: python:3.11-slim     # what containers start from: Python 3.10 or newer (and pip, for setup: build)
  setup: build                # build: testbench builds an image from `image` with its basics and `pip:`, once per
  pip: []                     #   config (tagged testbench-env:<hash>); none: use `image` as it is - it must already
                              #   have ipykernel, jupyter_client, nbformat, nbclient and pyyaml
  network: bridge             # bridge: Docker's default network; none: no network at all (a sandbox); host: this
                              #   machine's network; or a network made with `docker network create`
  gpus: null                  # what `docker run --gpus` takes: all, 2, "device=0,1" (needs NVIDIA's Container Toolkit)
  cpu: null                   # --cpus, e.g. 4
  memory_gb: null             # --memory, e.g. 16
  usd_per_hour: 0.0           # what the machine costs you while it works (0: your own); prices the worst case
  python: python3             # the image's Python command
  run_args: []                # more `docker create` arguments, e.g. [-v, /data:/data:ro] (also a string)
  build_args: []              # more `docker build` arguments, e.g. [--network, host] (also a string)
  ca_bundle: null             # a CA bundle (PEM) for pip behind a TLS-intercepting proxy: used to build the image, and
                              #   set as PIP_CERT in containers (a path relative to the project, or absolute)
  command: [docker]           # the container CLI
```

Use it like any other backend: `backend: docker` on a plan, `testbench session start --backend docker`, or
`sessions: {backend: docker}`. `testbench doctor` checks that the docker CLI is installed and the daemon answers;
`testbench prices` shows the rate. The section is checked only when a plan or the sessions use it, and every
error says what to write instead.

## How a run goes

1. The image: with `setup: build`, the first run builds `testbench-env:<hash>` from `image` (testbench's basics and
   `pip:`); later runs with the same config find it by its tag. Changing `image`, `pip`, `python` or `ca_bundle`
   builds a new one. The image's user is root. `docker image rm` an image to rebuild it (to pick up a newer base
   image under the same tag), and `docker image prune --filter label=agent-testbench` to clean old ones.
2. The container: `docker create --rm --init --name testbench-<project>-<run-id>`, with the limits above and
   labels `agent-testbench.project` and `agent-testbench.run`. The project (without `exclude:`), this package and the
   plan are copied in with `docker cp`, so the package always matches the testbench that started the run.
3. Inside, the project is at `/work/project` (also `/content`, for Colab notebooks), the working directory. The
   executor writes the run folder at `/work/run`; its progress and step logs come back to the local run folder
   every 2 seconds (`testbench run --wait`, `testbench wait` and the MCP server's progress follow them live), and
   everything else (executed notebooks, images, artifacts) at the end. Then the container is removed.
4. In the local run folder, next to the usual evidence: `docker.log` (every docker command testbench ran, and the
   image build's output), `container.log` (what the container printed) and `watchdog/` (the hard stop's log).

Nothing from this machine is mounted. Files go in and come out with `docker cp`, so what comes back is owned by
you, not by the container's root, and nothing depends on the daemon seeing this machine's folders (as with rootless
Docker or a remote `DOCKER_HOST`). To give a container data, mount it yourself, read-only:
`run_args: [-v, /data/imagenet:/data:ro]`.

Sessions work the same way in a long-running container (`testbench-<project>-<session>-<hash>`): the kernel runs
in `/work/project`; `exec` goes through `docker exec`; images a cell displays come back as files in the session's
`outputs/`; `files ls/get/put` and `session shell` work in the container; `session install` runs pip there;
`session sync` copies the project in again and keeps the kernel's variables; `session stop` removes the container.

## Secrets

`secrets:` on a step (or `--secret` on a session) reach the container as `-e NAME`, without a value: docker reads
the value from testbench's environment, so it never appears on a command line, in the process list, in
`docker.log` or in the container's command. A secret that is not in the environment refuses the run with a message
before anything starts. Like any container environment, `docker inspect` shows the values to whoever can use
Docker - who can already read everything on the machine.

## The hard stop

Every run and session has a stop inside the container, which holds even if testbench on this machine has stopped
(the driver killed, the terminal closed):

- A plan: the container's main process starts kernelkit's watchdog before the executor. At the plan's total
  `max_minutes` plus 2 minutes (the start-up allowance in the worst case) the watchdog interrupts the executor, which
  writes a cancelled report, and ends it 40 seconds later if it has not stopped. Each step also has its own
  `max_minutes`, enforced by the executor. Once the work ends, the container waits, idle, up to 2 minutes for its
  evidence to be taken, then ends; `--rm` removes it.
- A session: the watchdog ends the kernel at `max_minutes` + 2, or after `idle_minutes` + 10 without a command (a
  running cell or shell command counts as activity); the container lives exactly as long as its kernel, so it ends
  and is removed with it. The supervisor on this machine stops it sooner, at `idle_minutes` / `max_minutes`.
- `testbench cancel` interrupts the executor (`docker kill --signal INT`), takes what it wrote, and removes the
  container; if the run's driver is gone, it removes the container named in state.json.

A first image build happens before the container exists, so the hard stop does not cover it; it is bounded by a
one-hour timeout on this machine. To find anything left over: `docker ps -a --filter label=agent-testbench`.

## What it isolates, and what it does not

It isolates:

- **Files.** The container sees a copy of the project and nothing else of this machine's. What the code writes
  stays in the container; only the run folder comes back, and the container is deleted.
- **Network**, with `network: none`: only a loopback interface, so nothing can be downloaded or sent anywhere. The
  default `bridge` has its own network namespace but reaches the internet, your local network and services on this
  machine through the bridge's gateway; `host` is not isolated at all.
- **Processes.** The container has its own process namespace: it cannot see or signal this machine's processes.
- **Resources**, when you set them: `cpu` and `memory_gb` are hard limits (a process over the memory limit is killed:
  a notebook's kernel shows as a failed cell, the executor as a run that ended before the plan finished).
- **Time**: the hard stop above.

It does not:

- **It is not a virtual machine.** Containers share this machine's kernel, so a kernel or container-runtime
  exploit escapes. For code you have reason to think is hostile, use a VM, or gVisor
  (`run_args: [--runtime, runsc]`).
- **Root inside is root, confined.** Code runs as root in the container (the user of the image testbench builds;
  with `setup: none`, your image's own user); with rootful Docker that is this machine's root, limited by namespaces
  and dropped capabilities. Anything you mount with `run_args` is writable by it unless you
  mount it `:ro`. Never mount the Docker socket, and never add `--privileged`.
- **Disk is not limited.** A run can fill Docker's disk; it is freed when the container is removed.
- **`bridge` does not stop data leaving.** Code with network access can send what it can read (the project copy,
  its secrets) anywhere. Use `network: none` for code you have not reviewed, and give it no secrets.
- **GPU memory is not isolated** between containers that share a GPU.
- **The cost is what you say it is.** `usd_per_hour` prices the worst case like any backend; testbench bounds the
  work, not the machine.

## GPUs

Install NVIDIA's Container Toolkit on the machine (`docker run --rm --gpus all ubuntu nvidia-smi` should work), use
an image with the CUDA libraries your code needs, and set `gpus:`. A plan's or session's `gpu:` is then a label
(the report records the GPU the code actually used, sampled with nvidia-smi); a `gpu:` with no `docker.gpus` is an
error, because the container would get no GPU.

```yaml
docker:
  image: pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
  gpus: all
  memory_gb: 48
  usd_per_hour: 0.0           # your own box; or what the rented machine bills
plans:
  train:
    backend: docker
    gpu: RTX4090              # a label for the report; docker.gpus picks the devices
    steps:
      - {name: gpu check, run: "nvidia-smi && python -c 'import torch; assert torch.cuda.is_available()'", max_minutes: 2}
      - {name: train, notebook: train.ipynb, max_minutes: 60, artifacts: [checkpoints/*.pt]}
```

## Examples

A sandbox for code an agent just wrote:

```yaml
docker:
  network: none
  cpu: 2
  memory_gb: 4
plans:
  sandboxed:
    backend: docker
    steps:
      - {name: tests, run: "python -m pytest -q", max_minutes: 10}
```

Packages in the image (built once), and data mounted read-only:

```yaml
docker:
  image: python:3.12-slim
  pip: [numpy==2.1.3, pandas==2.2.3, scikit-learn==1.5.2]
  run_args: [-v, /srv/datasets:/data:ro]
```

An image that already has everything, used as it is:

```yaml
docker:
  image: ghcr.io/me/research-env:2026-10
  setup: none
  python: python
```

Behind a proxy: testbench passes `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` and `ALL_PROXY` (when they are set) to the
image build as build arguments, by name. A proxy on this machine's loopback needs the build to share this machine's
network, and a TLS-intercepting proxy needs its CA bundle. Containers get the CA bundle as `PIP_CERT`, but not the
proxy variables: add them to `run_args` by name if `session install` needs them.

```yaml
docker:
  build_args: [--network, host]
  ca_bundle: /etc/ssl/corp-ca.pem
  network: host                       # only if containers need the proxy too; it gives up network isolation
  run_args: [-e, HTTPS_PROXY, -e, HTTP_PROXY, -e, NO_PROXY]
```

Podman's CLI takes the same commands, so `command: [podman]` should work (it needs catatonit for `--init`); the
tests run against Docker only.
