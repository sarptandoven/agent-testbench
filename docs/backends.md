# Backends

A backend is the machine a plan or a live session runs on. A plan without `backend:` runs on `local`, and so does a
session started without `--backend`, unless testbench.yaml sets `sessions: {backend: B}`.

| backend | the machine | hard stop | |
|---|---|---|---|
| `local` | this one, in the project folder | this machine's supervisor | free |
| `docker` | a fresh container here; `network: none` for a sandbox, `gpus: all` for NVIDIA GPUs | a watchdog inside the container | [Docker](docker.md) |
| `ssh` | any machine `ssh` reaches: a GPU box, a university server, a rented instance | a watchdog on that machine | [ssh](ssh.md) |
| `kubernetes` | a Job on your cluster's GPU or CPU nodes, through kubectl | the Job's `activeDeadlineSeconds`, and a watchdog in the pod | [Kubernetes](kubernetes.md) |
| `runpod` | a pod of its own on RunPod's GPUs (or a CPU pod), through RunPod's API | the pod terminates itself at its priced worst case, with the key RunPod gives every pod | [RunPod](runpod.md) |
| `modal` | a [Modal](https://modal.com) Sandbox or function with any GPU (T4 ... B200) | Modal's own timeouts | your Modal account |
| `colab` | a [Colab](https://colab.research.google.com) VM through Google's Colab CLI | a watchdog on the VM, which releases it | your compute units |
| plug-ins | anything a package registers (`agent_testbench.backends` entry point) | the backend's own (`testbench plugin test` checks it has one) | [Plug-ins](plugins.md) |

Each is set per plan (`backend:`) or per session (`--backend`), so the same notebook can be proved for free
locally, then on the target GPU for a few cents. Linux, macOS and Windows (Colab's CLI has no Windows build).

## What each one needs

<!-- From README.md, "What each machine needs". Edit it there. -->
--8<-- "README.md#what-each-machine-needs"

## Read more

- [Docker](docker.md): sandboxes with no network, NVIDIA GPUs, what a container isolates and what it does not.
- [ssh](ssh.md): any machine you reach with ssh, with the watchdog there as the hard stop.
- [Kubernetes](kubernetes.md): plans and sessions as Jobs on a cluster's GPUs, stopped by the Job's own deadline.
- [RunPod](runpod.md): plans and sessions in pods of their own on RunPod's GPUs, each terminating itself at its
  priced worst case.
- [Plug-ins](plugins.md): a backend of your own, from an installed package.
- Modal and Colab: the GPUs each takes are under [plans](reference.md#plans), their sections of testbench.yaml
  under [top level](reference.md#top-level), and how sessions run and stop there under
  [live sessions](reference.md#live-sessions).
- What every price is and where it comes from: `testbench prices` ([prices](reference.md#prices)).
