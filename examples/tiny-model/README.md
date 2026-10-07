# tiny-model

A Colab-style notebook (`tiny_model.ipynb`, built from `cells/`) that trains a small neural network on
scikit-learn's bundled digits, saves `outputs/metrics.json` and a confusion-matrix plot, and offers the
metrics for download. Its form fields (`HIDDEN`, `EPOCHS`, `LEARNING_RATE`, `SEED`) are set per plan.

```bash
pip install "agent-testbench[all]" scikit-learn==1.5.2 matplotlib==3.9.2
testbench plans
testbench run smoke          # lint + quick train here, free, seconds
testbench run full-modal     # full train in a Modal CPU container (~$0.002)
testbench run gpu-check      # nvidia-smi + train on a Modal T4 (~$0.013) - the report shows the GPU sat idle
testbench run colab-cpu      # same notebook on a Colab CPU runtime (needs the Colab CLI)
```

Or work in it live, the way you would in Colab:

```bash
testbench session start                                   # or: --backend modal --gpu T4, or --backend colab
testbench exec --cells tiny_model.ipynb:1-3 --params '{"EPOCHS": 10}'
testbench exec "model.n_iter_, model.score(X_test, y_test)"
testbench session stop
```

Try breaking it (`params: {HIDDEN: 0}` in a plan, or `LEARNING_RATE = 1.0` in `cells/02_settings.py` and
`testbench nb build cells tiny_model.ipynb`) and read what the report says.
