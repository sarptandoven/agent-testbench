# The runpod backend

Plans and live sessions on [RunPod](https://www.runpod.io) GPUs. Every run is a pod of its own, made through
RunPod's REST API on the GPU the plan names, priced before it is made, and terminated when the run ends. If
testbench on this machine stops (a laptop lid closes, the network goes), the pod terminates itself at the plan's
worst case: the hard stop is on RunPod's side.

```yaml
runpod:
  image: python:3.11-slim      # what the pod runs: Python 3.10 or newer (and pip, for setup: pip)
  setup: pip                   # pip: the pod installs testbench's basics and `pip:` when it starts;
  pip: []                      #   none: the image already has ipykernel, jupyter_client, nbformat, nbclient, pyyaml
  python: python3              # the image's Python command
  cloud: SECURE                # SECURE (RunPod's data centers) or COMMUNITY (cheaper hosts)
  disk_gb: 20                  # the pod's container disk
  cpu_flavor: cpu3c            # a pod without a GPU: its CPU flavor ...
  vcpus: 2                     #   ... and vCPUs (a power of two)
  data_centers: []             # where it may run, e.g. [US-KS-2]; empty: anywhere
  env: {}                      # plain environment variables for the pod, e.g. {HF_HOME: /work/cache/hf}
  gpu_types: {}                # more of RunPod's GPUs: {RTXPRO6000: "NVIDIA RTX PRO 6000 ..."} (price each below)
  allow_without_remote_stop: false   # see "The hard stop"
  api_url: https://api.runpod.io/v2
  proxy_url: https://{pod_id}-{port}.proxy.runpod.net
```

Use it like any other backend: `backend: runpod` on a plan, `testbench session start --backend runpod`, or
`sessions: {backend: runpod}`. The section is checked only when a plan or the sessions use it.

## Setting up

Create an API key at runpod.io (Settings, API Keys; read and write) and export it as `RUNPOD_API_KEY`. testbench
reads it from the environment by name; it is never written to testbench.yaml, state.json, a log or a report, and
it is never sent to the pod. `testbench doctor` says whether it is set (not what it is).

The key only goes to RunPod's own hosts over https (a host under runpod.io, runpod.ai or runpod.net), or to this
machine over http (127.0.0.1, for testbench's own tests): any other `api_url` or `proxy_url` is refused when
testbench.yaml loads, so no edit of the file can send the key elsewhere.

## GPUs and prices

A plan names a GPU by a short name (`gpu: H100-SXM`, or `H100-SXM:2` for two); testbench asks RunPod for its GPU
type id. A plan without `gpu:` runs on a CPU pod (`cpu_flavor`, `vcpus`).

| `gpu:` | RunPod's GPU id | $/h per GPU (built in) |
|---|---|---|
| `RTX3090` | NVIDIA GeForce RTX 3090 | 0.60 |
| `RTX4090` | NVIDIA GeForce RTX 4090 | 0.90 |
| `RTX5090` | NVIDIA GeForce RTX 5090 | 1.20 |
| `A4000`, `A5000`, `A6000` | NVIDIA RTX A4000 / A5000 / A6000 | 0.45, 0.50, 0.95 |
| `A40` | NVIDIA A40 | 0.60 |
| `L4`, `L40`, `L40S` | NVIDIA L4 / L40 / L40S | 0.60, 1.25, 1.20 |
| `RTX6000-ADA` | NVIDIA RTX 6000 Ada Generation | 1.05 |
| `A100-PCIE`, `A100-SXM` | NVIDIA A100 80GB PCIe / NVIDIA A100-SXM4-80GB | 2.10, 2.40 |
| `H100-PCIE`, `H100-SXM`, `H100-NVL` | NVIDIA H100 PCIe / NVIDIA H100 80GB HBM3 / NVIDIA H100 NVL | 3.00, 3.80, 3.60 |
| `H200` | NVIDIA H200 | 5.00 |
| `B200` | NVIDIA B200 | 7.50 |
| `MI300X` | AMD Instinct MI300X OAM | 3.50 |
| (no `gpu:`) | a CPU pod | 0.08 per vCPU |

**Where these prices come from.** RunPod's pricing page could not be reached when they were written (2026-11-12),
so they are its Secure Cloud on-demand rates as last known, each raised 25-50% and rounded up: an estimate stays
above what RunPod charges (Community Cloud is cheaper still), and the margin covers the container disk. The GPU ids
were not checked against a live account either. `testbench prices` shows every price in use and where it comes
from; `testbench prices --update` fetches the catalog (it can only raise a price). To use your own, or to price a
GPU you add:

```yaml
runpod:
  gpu_types: {RTXPRO6000: "NVIDIA RTX PRO 6000 Blackwell Server Edition"}   # RunPod's id for it
pricing:
  runpod_usd_per_hour: {RTXPRO6000: 2.20, H100-SXM: 2.99}
  runpod_cpu_usd_per_vcpu_hour: 0.05
```

A GPU with no price is refused when testbench.yaml loads, so every launch is priced: the worst case is the GPU's
rate times the plan's steps' `max_minutes` plus the 10-minute start-up allowance, and `budget.check` refuses it
before anything reaches RunPod.

## How a run goes

1. testbench checks the key and the plan's secrets, then asks RunPod for a pod named `tb-<project>-<run-id>` with
   the plan's GPU, `image`, `cloud`, `disk_gb`, port 8000 behind RunPod's HTTP proxy, and a start command of its
   own: a standard-library Python script (the bootstrap) with the pod's deadline in it.
2. While the pod is provisioning testbench says so; a pod RunPod cannot start (ERROR), or one not running and set
   up within the start-up allowance (10 minutes: finding a machine, pulling the image, `setup: pip`), ends the run
   with what to do, and is terminated.
3. The bootstrap serves a small agent on port 8000, which RunPod's proxy reaches at
   `https://<pod-id>-8000.proxy.runpod.net`. Every request carries a token (an HMAC of your key and a nonce), and
   the agent answers nothing else; the token is taken out of the environment before any code runs. Through it,
   the project (without `exclude:`), this package and the plan go in as one tar.gz.
4. The executor runs in `/work/project` (also `/content`, for Colab notebooks), under kernelkit's watchdog. Its
   progress and step logs come back every few seconds, so `testbench run --wait`, `testbench wait` and the MCP
   server follow them live; everything else (executed notebooks, images, artifacts) comes back at the end.
5. testbench tells the pod its evidence was taken and terminates it (DELETE `/v2/pods/<id>`), and checks it is
   gone.

Why this way: the project goes in over HTTPS through RunPod's own proxy, so the image needs nothing but Python -
no sshd, no public IP, no key pair - and it works the same on Secure and Community Cloud. Each request to the agent
waits at most 25 seconds (RunPod's proxy ends a request at 100), so long commands are polled, not held open.

In the local run folder, next to the usual evidence: `runpod.log` (every API request - method, path, status - and
every command sent to the pod, never what went in), `pod.log` (what the bootstrap and the work printed) and
`watchdog/`. Keep large files out of the project (`exclude:`) and large artifacts out of the evidence: both go
through the proxy. Data the code needs comes from the image or the network.

## The hard stop

RunPod has no deadline of its own for a pod (its `terminateAfter` field was never enforced), so the pod ends
itself:

- The bootstrap is the pod's first process, and it holds the deadline from the pod's first second: the plan's
  worst case, the same minutes the budget priced, counted from when the pod was made. At the deadline it has RunPod
  terminate the pod, with the API key RunPod gives every pod for itself (`RUNPOD_API_KEY` in the pod, scoped to
  that pod, with `RUNPOD_POD_ID`): REST `DELETE /v2/pods/<id>`, then GraphQL `podTerminate`, then `runpodctl`.
- It also ends the pod when nothing was started in it within the start-up allowance (the run never got going:
  this machine went away after making it), and once the work is over: at once when testbench has taken the
  evidence, or after 2 minutes if nobody does.
- kernelkit's watchdog in the pod stops the work one minute before the deadline, so the executor writes its report
  and the evidence can still be taken. Each step also has its own `max_minutes`, enforced by the executor.
- If RunPod refuses every way, the bootstrap ends every process testbench started and exits, so the container
  stops with nothing running in it; the pod is then terminated by testbench here, or by you at
  runpod.io/console/pods.
- `testbench cancel` interrupts the executor (it writes a cancelled report), takes what it wrote, and terminates
  the pod; if the run's driver is gone, it terminates the pod named in state.json.
- A session's pod has the deadline `max_minutes` + 10; its watchdog also ends the kernel after `idle_minutes` + 10
  without a command, and the pod terminates itself with its kernel. The supervisor on this machine stops it sooner.

Before anything is copied in, the pod says which ways it has to end itself. A pod with none (no `RUNPOD_POD_ID`
and `RUNPOD_API_KEY` of its own, no `runpodctl`) would bill until someone ended it if this machine slept, so the run
is refused and the pod terminated at once. `runpod.allow_without_remote_stop: true` runs it anyway, with testbench
on this machine as its only stop (the report's `remote_stop` says so); the Claude Code guard hook asks the person
before an agent's edit turns it on.

To find anything left over: pods named `tb-...` at runpod.io/console/pods. Keep RunPod's own spend limit set too.

## Secrets

A step's `secrets:` (or a session's `--secret`) go to the pod by name, as its environment variables in the request
that makes it, read from this machine's environment. Their values are not in testbench.yaml, state.json,
`runpod.log` or the report, and a value the code prints is replaced by `[secret]` in what the run keeps. RunPod
keeps a pod's environment with the pod, as it does for any pod you make.

## Live sessions

`testbench session start --backend runpod [--gpu RTX4090]` makes a pod the same way and starts a kernel in it;
`exec`, `files`, `session shell`, `session sync` and `session install` go through the same agent. state.json keeps
the pod's id and the nonce, never the key or the token: each command recomputes the token from `RUNPOD_API_KEY`,
so it must be set wherever you run them. `testbench session stop` terminates the pod.

## What is known about RunPod's API, and what is not

Written without a RunPod account and without docs.runpod.io (not reachable from where this was written), from
RunPod's own REST v1 to v2 migration guide and public reports:

- Known: REST v2 at `https://api.runpod.io/v2` (v1 at rest.runpod.io retires in November 2026), Bearer auth,
  `POST /pods` with `name`, `image`, `gpu: {id, count}` or `cpu: {id, vcpuCount}`, `cloud`, `disk`, `env`, `ports`
  (no default in v2), `entrypoint` / `cmd`, `dataCenterIds`; `GET /pods/<id>` with `status` (PROVISIONING,
  STARTING, RUNNING, EXITED, ERROR, TERMINATED); `DELETE /pods/<id>`; `GET /pods` as `{"pods": [...]}`. Every pod
  has `RUNPOD_POD_ID` and a pod-scoped `RUNPOD_API_KEY`; a pod terminating itself with GraphQL `podTerminate` and
  that key has been reported working.
- Not checked against a live account: the GPU ids in the table, the prices, that REST `DELETE` takes the
  pod-scoped key (GraphQL and runpodctl are tried after it), how long a start command may be (the bootstrap is
  about 9 KB), and what a failed create returns. testbench's own tests run against a fake RunPod on 127.0.0.1.
