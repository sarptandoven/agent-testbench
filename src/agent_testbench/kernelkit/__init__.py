"""The live kernel behind a session: a launcher that keeps one IPython kernel running on the session's machine,
and a client that runs code in it and brings back what it printed, displayed and raised. The same two pieces run
on this machine, in a Modal Sandbox and on a Colab VM, so a session behaves the same everywhere."""
