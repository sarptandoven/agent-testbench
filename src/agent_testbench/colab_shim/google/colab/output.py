"""Front-end helpers do nothing headless."""


def clear(wait=False, output_tags=()):
    pass


def enable_custom_widget_manager():
    pass


def disable_custom_widget_manager():
    pass


def eval_js(script, ignore_result=False, timeout_sec=None):
    raise RuntimeError("google.colab.output.eval_js needs a browser; this run is headless")


def serve_kernel_port_as_window(port, path="/", anchor_text=None):
    print(f"[testbench] app on port {port} (no browser window headless)", flush=True)


def serve_kernel_port_as_iframe(port, path="/", height=400, cache_in_notebook=False):
    serve_kernel_port_as_window(port, path)
