from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

QED_PROBLEMS = [
    {
        "id": "sqrt2",
        "prompt": "Generate a rigorous proof that the square root of 2 is irrational.",
    },
    {
        "id": "odd_sum",
        "prompt": (
            "Generate a rigorous proof that for every positive integer n, "
            "1 + 3 + 5 + ... + (2n-1) = n^2."
        ),
    },
    {
        "id": "inf_primes",
        "prompt": "Generate a rigorous proof that there are infinitely many prime numbers.",
    },
    {
        "id": "cube_identity",
        "prompt": (
            "Let a, b, c be real numbers with a+b+c=0. Generate a rigorous proof that "
            "a^3+b^3+c^3=3abc."
        ),
    },
    {
        "id": "am_gm_two",
        "prompt": (
            "Generate a rigorous proof that for all positive real x and y, "
            "(x+y)/2 is at least sqrt(xy), with equality exactly when x=y."
        ),
    },
]


DEEPSEEK_PROBLEMS = [
    {
        "id": "add_zero",
        "formal": """import Mathlib
import Aesop
set_option maxHeartbeats 0
theorem rk_ds_add_zero (n : Nat) : n + 0 = n := by
  sorry""",
    },
    {
        "id": "add_comm",
        "formal": """import Mathlib
import Aesop
set_option maxHeartbeats 0
theorem rk_ds_add_comm (a b : Nat) : a + b = b + a := by
  sorry""",
    },
    {
        "id": "sq_nonneg",
        "formal": """import Mathlib
import Aesop
set_option maxHeartbeats 0
theorem rk_ds_sq_nonneg (x : Real) : 0 ≤ (x - 1)^2 := by
  sorry""",
    },
    {
        "id": "reverse_reverse",
        "formal": """import Mathlib
import Aesop
set_option maxHeartbeats 0
theorem rk_ds_reverse_reverse {A : Type} (xs : List A) : xs.reverse.reverse = xs := by
  sorry""",
    },
    {
        "id": "official_abs",
        "formal": """import Mathlib
import Aesop
set_option maxHeartbeats 0
open BigOperators Real Nat Topology Rat
theorem rk_ds_official_abs : abs ((120 : Real) / 100 * 30 - 130 / 100 * 20) = 10 := by
  sorry""",
    },
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_revision(model_path: Path) -> str | None:
    revisions: set[str] = set()
    for metadata in (model_path / ".cache" / "huggingface" / "download").glob("*.metadata"):
        lines = metadata.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines and re.fullmatch(r"[0-9a-f]{40}", lines[0]):
            revisions.add(lines[0])
    return next(iter(revisions)) if len(revisions) == 1 else None


def deepseek_prompt(formal: str) -> str:
    return f"""Complete the following Lean 4 code:

```lean4
{formal}
```

Before producing the Lean 4 code to formally prove the given theorem, provide a detailed proof
plan outlining the main proof steps and strategies. The plan should highlight key ideas,
intermediate lemmas, and proof structures that will guide the construction of the final formal
proof."""


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in {"qed", "deepseek-prover"}:
        raise SystemExit("usage: rkmodelbench.py qed|deepseek-prover MODEL_PATH OUTPUT_DIR")
    kind = sys.argv[1]
    model_path = Path(sys.argv[2]).resolve()
    output_dir = Path(sys.argv[3]).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    effective_generation = (
        {
            "max_new_tokens": 32768,
            "do_sample": True,
            "temperature": 0.6,
            "top_k": 20,
            "top_p": 0.95,
        }
        if kind == "qed"
        else {"max_new_tokens": 8192, "do_sample": False}
    )
    (output_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "kind": kind,
                "model_path": str(model_path),
                "model_revision": snapshot_revision(model_path),
                "generation": effective_generation,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    torch.manual_seed(30)
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
    problems = QED_PROBLEMS if kind == "qed" else DEEPSEEK_PROBLEMS
    results: list[dict[str, Any]] = []
    for problem in problems:
        prompt = problem["prompt"] if kind == "qed" else deepseek_prompt(problem["formal"])
        chat = [{"role": "user", "content": prompt}]
        inputs = tokenizer.apply_chat_template(
            chat,
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
            else:
                outputs = model.generate(**inputs, max_new_tokens=8192)
        elapsed = time.monotonic() - started
        input_tokens = int(inputs["input_ids"].shape[-1])
        generated = outputs[0][input_tokens:]
        text = tokenizer.decode(generated, skip_special_tokens=True)
        text_path = output_dir / f"{problem['id']}.txt"
        text_path.write_text(text, encoding="utf-8")
        if kind == "deepseek-prover":
            (output_dir / f"{problem['id']}.statement.lean").write_text(
                str(problem["formal"]) + "\n", encoding="utf-8"
            )
        results.append(
            {
                "id": problem["id"],
                "input_tokens": input_tokens,
                "output_tokens": int(generated.shape[-1]),
                "wall_seconds": elapsed,
                "tokens_per_second": float(generated.shape[-1]) / elapsed,
                "peak_gpu_bytes": int(torch.cuda.max_memory_allocated()),
                "hit_token_limit": int(generated.shape[-1])
                == int(effective_generation["max_new_tokens"]),
                "output_sha256": sha256(text_path),
            }
        )
        (output_dir / "progress.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    receipt = {
        "kind": kind,
        "model_path": str(model_path),
        "model_revision": snapshot_revision(model_path),
        "load_seconds": load_seconds,
        "torch": torch.__version__,
        "hip": torch.version.hip,
        "gpu": torch.cuda.get_device_name(0),
        "dtype": "bfloat16",
        "seed": 30,
        "generation": effective_generation,
        "results": results,
    }
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "COMPLETED",
                "kind": kind,
                "model_revision": receipt["model_revision"],
                "result_count": len(results),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
