from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def deepseek_prompt(formal: str) -> str:
    return f"""Complete the following Lean 4 code:

```lean4
{formal}
```

Before producing the Lean 4 code to formally prove the given theorem, provide a detailed proof
plan outlining the main proof steps and strategies. The plan should highlight key ideas,
intermediate lemmas, and proof structures that will guide the construction of the final formal
proof."""


def revision(model_path: Path) -> str | None:
    revisions: set[str] = set()
    for metadata in (model_path / ".cache" / "huggingface" / "download").glob("*.metadata"):
        lines = metadata.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines and len(lines[0]) == 40:
            revisions.add(lines[0])
    return next(iter(revisions)) if len(revisions) == 1 else None


def main() -> int:
    if len(sys.argv) != 6 or sys.argv[1] not in {"qed", "deepseek-prover"}:
        raise SystemExit(
            "usage: rklocalmodelinvoke.py qed|deepseek-prover MODEL_PATH SEED INPUT OUTPUT"
        )
    kind = sys.argv[1]
    model_path = Path(sys.argv[2]).resolve()
    seed = int(sys.argv[3])
    input_path = Path(sys.argv[4]).resolve()
    output_path = Path(sys.argv[5]).resolve()
    value = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != ({"prompt"} if kind == "qed" else {"formal"}):
        raise SystemExit("input object has unexpected keys")
    source = value["prompt"] if kind == "qed" else deepseek_prompt(value["formal"])
    if not isinstance(source, str) or not source.strip():
        raise SystemExit("model input must be a non-empty string")
    torch.manual_seed(seed)
    load_started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        local_files_only=True,
        device_map="auto",
        dtype=torch.bfloat16,
        trust_remote_code=True,
    )
    load_seconds = time.monotonic() - load_started
    inputs = tokenizer.apply_chat_template(
        [{"role": "user", "content": source}],
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    ).to(model.device)
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    with torch.inference_mode():
        if kind == "qed":
            outputs = model.generate(
                **inputs,
                max_new_tokens=32768,
                do_sample=True,
                temperature=0.6,
                top_k=20,
                top_p=0.95,
            )
            generation: dict[str, Any] = {
                "max_new_tokens": 32768,
                "do_sample": True,
                "temperature": 0.6,
                "top_k": 20,
                "top_p": 0.95,
            }
        else:
            outputs = model.generate(**inputs, max_new_tokens=8192)
            generation = {"max_new_tokens": 8192, "do_sample": False}
    generation_seconds = time.monotonic() - started
    input_tokens = int(inputs["input_ids"].shape[-1])
    generated = outputs[0][input_tokens:]
    text = tokenizer.decode(generated, skip_special_tokens=True)
    result = {
        "schema_version": "rk.local-proof-model.v1",
        "kind": kind,
        "model_revision": revision(model_path),
        "seed": seed,
        "generation": generation,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": int(generated.shape[-1]),
            "load_wall_seconds": load_seconds,
            "generation_wall_seconds": generation_seconds,
            "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()),
            "hit_token_limit": int(generated.shape[-1]) == int(generation["max_new_tokens"]),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "COMPLETED", "usage": result["usage"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
