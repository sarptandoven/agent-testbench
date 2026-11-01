"""An agent of your own, in one file: Claude (the Anthropic SDK) works on a project with agent-testbench's tools until
a plan passes, then says which run proves it.

    pip install anthropic "agent-testbench[mcp]"
    python agent_loop.py ../tiny-model smoke

The tools are the MCP server's (agent_testbench.tools), so every launch stays inside the project's budget and this
machine's limits. A cost above the approval threshold is put to you at the terminal; the model cannot approve it.
"""
import sys

from agent_testbench import tools

MODEL = "claude-opus-5-5"
SYSTEM = ("You work on a software project through agent-testbench's tools. Explore in a live session, fix the code, "
          "and prove the result with a plan run - never with 'it should work'. Stop sessions when done. When the "
          "plan passes, say which run proves it and what it cost.")


def ask_person(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def work(client, project_dir: str, plan: str, max_turns: int = 40, ask=ask_person, say=print) -> str:
    defs = tools.anthropic_tools(project_dir=project_dir)          # the model works in this project only
    messages = [{"role": "user", "content": f"Make the {plan} plan pass and prove it."}]
    for _ in range(max_turns):
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000, system=SYSTEM, tools=defs, messages=messages,
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")   # a declined request is retried
        messages.append({"role": "assistant", "content": response.content})
        text = "\n".join(b.text for b in response.content if b.type == "text")
        if text:
            say(text)
        if response.stop_reason == "refusal":
            return "The model declined this request."
        if response.stop_reason != "tool_use":
            return text
        results = []
        for block in response.content:
            if block.type == "tool_use":
                say(f"-> {block.name} {block.input}")
                out = tools.call(block.name, block.input, project_dir=project_dir, ask=ask)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})          # every result of the turn in one message
    return f"Stopped after {max_turns} turns."


if __name__ == "__main__":
    import anthropic
    project = sys.argv[1] if len(sys.argv) > 1 else "."
    plan = sys.argv[2] if len(sys.argv) > 2 else "smoke"
    work(anthropic.Anthropic(), project, plan)
