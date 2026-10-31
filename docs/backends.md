# Backends

A backend is the machine a plan or a live session runs on. A plan without `backend:` runs on `local`, and so does a
session started without `--backend`, unless testbench.yaml sets `sessions: {backend: B}`.

<!-- From README.md, "Where it runs" and "Install notes". Edit them there. -->
--8<-- "README.md#where-it-runs"

## What each one needs

--8<-- "README.md#install-notes"

## Read more

- [Docker](docker.md): sandboxes with no network, NVIDIA GPUs, what a container isolates and what it does not.
- [ssh](ssh.md): any machine you reach with ssh, with the watchdog there as the hard stop.
- [Plugins](plugins.md): a backend of your own, from an installed package.
- Modal and Colab: the GPUs each takes are under [plans](reference.md#plans), their sections of testbench.yaml
  under [top level](reference.md#top-level), and how sessions run and stop there under
  [live sessions](reference.md#live-sessions).
- What every price is and where it comes from: `testbench prices` ([prices](reference.md#prices)).
