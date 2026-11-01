"""A small customer-support bot: Claude answers questions about orders and looks them up with one tool.

    python support_bot.py "Where is my order A1002?"
    python support_bot.py --questions questions.json --out outputs/answers.json

Needs `pip install anthropic` and ANTHROPIC_API_KEY (or `ant auth login`). The testbench plan runs it against the
mock anthropic package instead: no key, no network, no cost.
"""
import argparse
import json
import sys
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"
SYSTEM = ("You are the support assistant of a small online shop. Answer questions about orders in one or two "
          "sentences. Look an order up with lookup_order before you say anything about it; never guess its status.")
ORDERS = json.loads((Path(__file__).parent / "orders.json").read_text())
LOOKUP_ORDER = {
    "name": "lookup_order",
    "description": "The status of one order (processing, shipped or cancelled), with the carrier and delivery date "
                   "once it has shipped and the refund once it was cancelled.",
    "input_schema": {"type": "object", "properties": {"order_id": {"type": "string", "description": "e.g. A1002"}},
                     "required": ["order_id"], "additionalProperties": False},
    "strict": True,
}


def lookup_order(order_id: str) -> dict:
    order_id = order_id.strip().upper()
    return {"order_id": order_id, **ORDERS.get(order_id, {"status": "not found"})}


def answer(client, question: str, max_turns: int = 5) -> dict:
    messages = [{"role": "user", "content": question}]
    looked_up = []
    for _ in range(max_turns):
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000, system=SYSTEM, tools=[LOOKUP_ORDER], messages=messages,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")     # a declined request is retried
        if response.stop_reason == "refusal":
            return {"question": question, "answer": "Sorry, I can't help with that.", "orders": looked_up}
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            text = " ".join(b.text for b in response.content if b.type == "text").strip()
            return {"question": question, "answer": text, "orders": looked_up}
        results = []
        for block in response.content:
            if block.type == "tool_use":
                looked_up.append(block.input["order_id"])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(lookup_order(block.input["order_id"]))})
        messages.append({"role": "user", "content": results})      # every result of the turn in one message
    return {"question": question, "answer": "Sorry, I could not finish looking that up.", "orders": looked_up}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("question", nargs="*")
    ap.add_argument("--questions", help="a JSON list of questions to answer")
    ap.add_argument("--out", help="write the answers here as JSON")
    a = ap.parse_args(argv)
    questions = a.question + (json.loads(Path(a.questions).read_text()) if a.questions else [])
    if not questions:
        ap.error("give a question, or --questions FILE")
    client = anthropic.Anthropic()
    answers = []
    for q in questions:
        answers.append(answer(client, q))
        print(f"Q: {q}\nA: {answers[-1]['answer']}\n", flush=True)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"questions": len(answers), "answers": answers,
                                           "lookups": sum(len(x["orders"]) for x in answers)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
