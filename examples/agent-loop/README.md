# agent-loop

`agent_loop.py`: an agent of your own in about 60 lines - a tool-use loop with the Anthropic SDK and
agent-testbench's tools (`agent_testbench.tools`), told to make a plan pass and prove it.

```bash
pip install anthropic "agent-testbench[mcp]"
python agent_loop.py ../tiny-model smoke      # PROJECT_DIR PLAN; uses ANTHROPIC_API_KEY (or `ant auth login`)
```

It runs Claude Opus 5.5 with server-side refusal fallbacks on. A launch above the project's approval threshold is
put to you at the terminal (`ask=`); the model cannot approve it. The Python API and the tools for the OpenAI API:
[docs/python-api.md](../../docs/python-api.md).
