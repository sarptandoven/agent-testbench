A stand-in for `google.colab`, put first on the kernel's PYTHONPATH when a notebook runs headless (locally, on
Modal, or on a Colab VM driven by the Colab CLI, where there is no browser to pick files or grant secrets).

`google/` deliberately has no `__init__.py`: it stays a namespace package, so `google.protobuf`, `google.cloud`
and the rest still import from site-packages next to it.
