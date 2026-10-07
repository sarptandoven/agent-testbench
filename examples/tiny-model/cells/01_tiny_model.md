# Tiny model: handwritten digits

A Colab-style notebook, and agent-testbench's first example. It trains a small neural network on scikit-learn's
bundled 8x8 digits (no download), saves its metrics and a confusion-matrix plot, and offers the metrics for
download. It runs as-is on Colab; `testbench run smoke` runs it headless on your machine, `testbench run full-modal`
on Modal.

The settings below are Colab form fields: a plan in `testbench.yaml` can set them (`params: {EPOCHS: 15}`).
