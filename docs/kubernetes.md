# The kubernetes backend

Plans and live sessions as Jobs on a Kubernetes cluster: the GPU cluster a team or a lab already has. Every run is
a Job with one pod, on a GPU node or a CPU one, priced before it starts and stopped by Kubernetes itself at its worst
case. Nothing is assumed of the cluster beyond kubectl access: no shared storage, no registry to push to, no
operator to install.

```yaml
kubernetes:
  context: null                # a context from your kubeconfig (null: kubectl's current one)
  namespace: null              # null: the context's namespace (or default)
  image: python:3.11-slim      # what the pod runs: Python 3.10 or newer (and pip, for setup: pip)
  setup: pip                   # pip: the pod installs testbench's basics and `pip:` when it starts;
  pip: []                      #   none: the image already has ipykernel, jupyter_client, nbformat, nbclient, pyyaml
  python: python3              # the image's Python command
  image_pull_policy: IfNotPresent   # IfNotPresent | Always | Never
  usd_per_hour: {cpu: 0.0}     # per GPU by name, and `cpu` for a pod without one: {cpu: 0.1, A100: 2.5, H100: 4.0}
  gpu_resource: nvidia.com/gpu # the resource a GPU is requested as (amd.com/gpu, ...)
  gpu_node_selector: {}        # where each GPU is: {A100: {nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB}}
  node_selector: {}            # for every pod, e.g. {pool: research}
  tolerations: []              # e.g. [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
  service_account: null        # null: no service account token in the pod
  cpu: null                    # the pod's CPU request and limit (a plan's own cpu: wins)
  memory_gb: null              # the pod's memory request and limit (a plan's own memory_gb: wins)
  env: {}                      # plain environment variables for the pod, e.g. {HF_HOME: /work/cache/hf}
  env_secrets: {}              # secrets from Kubernetes Secrets: {HF_TOKEN: hf-token/token} (SECRET/KEY)
  volumes: []                  # more pod volumes: [{name: data, persistentVolumeClaim: {claimName: imagenet}}]
  volume_mounts: []            # and where they go: [{name: data, mountPath: /data, readOnly: true}]
  command: [kubectl]           # how to run kubectl, e.g. [kubectl, --kubeconfig, /home/me/.kube/lab]
```

Use it like any other backend: `backend: kubernetes` on a plan, `testbench session start --backend kubernetes`, or
`sessions: {backend: kubernetes}`. The section is checked only when a plan or the sessions use it, and every error
says what to write instead. `testbench doctor` checks that kubectl is installed, that the context answers, and that
your account may create Jobs and exec into pods there; `testbench prices` shows the rates.

## GPUs and prices

The GPUs a plan can ask for are the keys of `usd_per_hour`, so every launch is priced: a plan whose `gpu:` has no
price is refused when testbench.yaml is read, and a session started with `--gpu` that has none is refused by the
budget. A GPU's price is for the whole pod that has it (its share of the node's CPU and memory too); `cpu` is the
price of a pod without a GPU. `gpu: A100:2` asks for two (`nvidia.com/gpu: 2`) at twice the price.

`gpu_resource` only asks for a number of GPUs. When the cluster has more than one kind, `gpu_node_selector` says how
to find each one, from the labels your nodes carry: NVIDIA's GPU feature discovery sets `nvidia.com/gpu.product`,
GKE sets `cloud.google.com/gke-accelerator`, EKS node groups often `node.kubernetes.io/instance-type`. Without it,
`gpu: A100` is the price you pay and the pod lands on whichever GPU node is free.

```yaml
kubernetes:
  image: pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
  usd_per_hour: {cpu: 0.10, A100: 2.20, H100: 3.90}
  gpu_node_selector:
    A100: {nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB}
    H100: {nvidia.com/gpu.product: NVIDIA-H100-80GB-HBM3}
  tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
  memory_gb: 64
  cpu: 8
plans:
  train:
    backend: kubernetes
    gpu: H100
    steps:
      - {name: gpu check, run: "nvidia-smi && python -c 'import torch; assert torch.cuda.is_available()'", max_minutes: 2}
      - {name: train, notebook: train.ipynb, max_minutes: 60, artifacts: [checkpoints/*.pt]}
```

## How a run goes

1. testbench fixes the context and namespace (so a later cancel finds the same Job), and creates a Job named
   `tb-<project>-<run-id>-<hash>`, labelled `agent-testbench/project`, `agent-testbench/job` and
   `app.kubernetes.io/managed-by: agent-testbench`. Its pod has an `emptyDir` at `/work` and starts a small
   standard-library script that waits for the project.
2. While the pod is pending, testbench says why (`0/3 nodes are available: 3 Insufficient nvidia.com/gpu`). An image
   that cannot be pulled or a Secret that does not exist ends the run at once with what to fix; a pod that has not
   started within the start-up allowance (5 minutes) ends it with the scheduler's last reason.
3. Once the pod runs, the project (without `exclude:`), this package and the plan go in with `kubectl exec -i`, as a
   tar.gz on stdin; then what to run, with secrets from this machine, the same way. With `setup: pip` the pod
   installs testbench's basics and `pip:` (each run: for speed, use an image that has them, with `setup: none`).
4. The executor runs in `/work/project` (also `/content`, for Colab notebooks). Its progress and step logs come
   back every 2 seconds, as a tar.gz on kubectl exec's stdout, so `testbench run --wait`, `testbench wait` and the
   MCP server follow them live; everything else (executed notebooks, images, artifacts) comes back at the end.
5. The pod waits, at most 3 minutes, until testbench has taken the evidence and says so; then the Job is deleted,
   and its pod with it.

In the local run folder, next to the usual evidence: `kubectl.log` (every kubectl command testbench ran, with its
exit code; never what went in on stdin), `pod.log` (what the pod printed: the start-up script, pip) and `watchdog/`.

Data the code needs comes from the image, from the network, or from a volume you mount:
`volumes: [{name: data, persistentVolumeClaim: {claimName: imagenet, readOnly: true}}]` with
`volume_mounts: [{name: data, mountPath: /data, readOnly: true}]`. The project itself always goes in as a copy;
keep large files out of it with `exclude:`.

## The hard stop

Kubernetes stops every run and session itself, so the bound holds even if testbench on this machine has stopped:

- The Job's `activeDeadlineSeconds` is the plan's worst case: its steps' `max_minutes` plus the 5-minute start-up
  allowance, the same minutes the budget priced. It counts from when the Job starts, pending time included; at the
  deadline Kubernetes ends the pod and marks the Job failed (`DeadlineExceeded`), and the report says so.
- `backoffLimit: 0` and `restartPolicy: Never`: a pod that fails is not tried again.
- `ttlSecondsAfterFinished: 600`: a finished Job is deleted even if testbench never deletes it.
- Inside the pod, kernelkit's watchdog stops the work one minute before the deadline, so the executor writes its
  report and the evidence can still be taken. Each step also has its own `max_minutes`, enforced by the executor.
- `testbench cancel` interrupts the executor (it writes a cancelled report), takes what it wrote, and deletes the
  Job; if the run's driver is gone, it deletes the Job named in state.json, in the context and namespace the run
  started with.
- A session's Job has the deadline `max_minutes` + 5; its watchdog also ends the kernel after `idle_minutes` + 10
  without a command, and the pod ends with its kernel. The supervisor on this machine stops it sooner.

To find anything left over: `kubectl get jobs -A -l app.kubernetes.io/managed-by=agent-testbench`.

## Secrets

A step's `secrets:` (or a session's `--secret`) reach the pod one of two ways, and their values never appear in the
Job, on a command line, in `kubectl.log` or in a report:

- Names in `env_secrets` come from a Kubernetes Secret in the namespace, by reference (`secretKeyRef`):
  `env_secrets: {HF_TOKEN: hf-token/token}` after `kubectl create secret generic hf-token --from-literal=token=...`.
  A Secret or key that does not exist stops the run before anything runs, with that message.
- Other names come from this machine's environment, sent over `kubectl exec`'s stdin into a file the pod reads and
  deletes before the work starts. A name in neither refuses the run before anything is created.

Only the secrets a plan's steps name reach its pod. Like any pod environment, whoever may exec into the pod can read
them there.

## What your account needs

In the namespace: create, get, list and delete Jobs; get and list pods; get pods/log; create pods/exec. A Role for
it:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: agent-testbench, namespace: research}
rules:
  - {apiGroups: [batch], resources: [jobs], verbs: [create, get, list, delete]}
  - {apiGroups: [""], resources: [pods], verbs: [get, list]}
  - {apiGroups: [""], resources: [pods/log], verbs: [get]}
  - {apiGroups: [""], resources: [pods/exec], verbs: [create]}
```

The pod itself gets no service account token unless you set `service_account` (for workload identity to a bucket,
or an image pull secret for a private registry). It runs as the image's user, and everything testbench writes is in
`/work`, an `emptyDir`; with `setup: pip`, that user must be able to install packages (root, a virtualenv's Python,
or a writable home for pip's user install).

## Without a package index

With `setup: pip` the pod needs to reach a package index. Behind a mirror, set pip's own variables in `env:`
(`PIP_INDEX_URL`, `PIP_TRUSTED_HOST`), or use an image that has everything (`setup: none`), built from
`python:3.11-slim` with `pip install ipykernel jupyter_client nbformat nbclient pyyaml` and your packages. The
docker backend builds one (`testbench-env:<hash>`); push it to your registry, or load it into a kind cluster.

## Trying it on kind

[kind](https://kind.sigs.k8s.io) runs a cluster in Docker on this machine. The opt-in tests in
`tests/test_kubernetes.py` run real plans, a deadline, a cancel and a session there:

```bash
kind create cluster --name testbench
TESTBENCH_TEST_KIND=1 python -m pytest tests/test_kubernetes.py      # builds and loads an image with the basics
```

`TESTBENCH_TEST_KIND_CLUSTER` names another cluster, `TESTBENCH_TEST_KIND_IMAGE` an image already on its node,
and `TESTBENCH_TEST_KIND_PLAIN_IMAGE` / `TESTBENCH_TEST_KIND_PIP_ENV` (JSON) the image and pip settings for the
`setup: pip` test when the pods have no package index. In a sandbox without `CAP_SYS_RESOURCE`, the cluster's own
pods never start (`runc create failed: ... can't get final child's PID from pipe`); create it with
`kind create cluster --name testbench --config kind.yaml` and this `kind.yaml`:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri"]
    restrict_oom_score_adj = true
```

Without a cluster, the unit tests in that file still check the Job testbench creates (its deadline, `backoffLimit`,
resources, node selectors, secret references), where secrets go, cancelling, and the failure messages, against a
stand-in for kubectl.
