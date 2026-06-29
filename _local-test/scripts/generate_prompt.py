#!/usr/bin/env python3
"""Generate a long filler prompt of a target token size with three hidden needles.

Used by the context-ceiling sweep. We embed three verifiable facts ("needles") at
~10%, ~50% and ~90% of the document, so a single run probes retrieval at the head,
middle and tail of the context window -- not just the middle. llama.cpp reports the
real prompt token count, so the char target here only needs to be roughly right.

Usage:
    python generate_prompt.py --tokens 16384 --out ..\\prompts\\p16384.txt
"""
import argparse

# Three needles with distinct, easy-to-grep passphrases. Position (fraction of the
# document) is applied in build(). The sweep checks each phrase independently.
NEEDLES = [
    (0.10, "IMPORTANT FACT (HEAD): The vault HEAD passphrase is 'aurora-head-7741'."),
    (0.50, "IMPORTANT FACT (MIDDLE): The vault MIDDLE passphrase is 'aurora-mid-7742'."),
    (0.90, "IMPORTANT FACT (TAIL): The vault TAIL passphrase is 'aurora-tail-7743'."),
]
QUESTION = (
    "\n\nBased on the document above, output the three vault passphrases as three "
    "lines, values only, in exactly this form:\n"
    "HEAD=<value>\nMIDDLE=<value>\nTAIL=<value>\n"
)

# A filler paragraph that varies by index so it isn't trivially compressible.
PARA = (
    "Section {i}. In the study of large language model serving, the dominant "
    "constraint at long context is not the parameter count but the growth of the "
    "key-value cache, which expands with every additional token in the sequence. "
    "Engineers managing memory budgets must weigh quantization of weights against "
    "quantization of the cache itself, since each saved byte on one side can be "
    "spent extending the usable window on the other. Record number {i} notes this "
    "trade-off carefully for later review. "
)

CHARS_PER_TOKEN = 3.8  # rough English estimate; we fill slightly under the target


def build(tokens: int) -> str:
    target_chars = int(tokens * CHARS_PER_TOKEN)
    # Char offset at which each needle should be inserted, in document order.
    pending = sorted(((int(frac * target_chars), text) for frac, text in NEEDLES),
                     key=lambda x: x[0])
    parts, total, i, ni = [], 0, 1, 0
    while total < target_chars:
        while ni < len(pending) and total >= pending[ni][0]:
            block = "\n" + pending[ni][1] + "\n"
            parts.append(block)
            total += len(block)
            ni += 1
        p = PARA.format(i=i)
        parts.append(p)
        total += len(p)
        i += 1
    # Any needles not placed yet (tiny target) go in just before the question.
    while ni < len(pending):
        parts.append("\n" + pending[ni][1] + "\n")
        ni += 1
    parts.append(QUESTION)
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokens", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    text = build(args.tokens)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {args.out}: {len(text)} chars (~{int(len(text)/CHARS_PER_TOKEN)} est tokens)")


if __name__ == "__main__":
    main()
