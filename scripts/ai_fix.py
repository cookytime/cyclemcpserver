#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv


def run_check(repo_root: Path, check_cmd: str, log_file: Path) -> int:
    result = subprocess.run(
        check_cmd,
        cwd=repo_root,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = result.stdout or ""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(output, encoding="utf-8")
    print(output, end="")
    return result.returncode


def parse_error_files(log_text: str, repo_root: Path) -> list[Path]:
    counts: Counter[Path] = Counter()
    patterns = [
        re.compile(r"^\s*-->\s*([^:]+\.py):\d+:\d+", re.MULTILINE),
        re.compile(r"^\s*([^:\s]+\.py):\d+:\d+:", re.MULTILINE),
        re.compile(r"^\s*([^:\s]+\.py):\d+:\d+\s+-\s+error:", re.MULTILINE),
    ]

    for pattern in patterns:
        for match in pattern.finditer(log_text):
            raw = match.group(1).strip()
            path = Path(raw)
            if path.is_absolute():
                try:
                    path = path.relative_to(repo_root)
                except ValueError:
                    continue
            full = repo_root / path
            if full.exists() and full.suffix == ".py":
                counts[path] += 1

    return [p for p, _ in counts.most_common()]


def build_prompt(
    files: list[Path],
    repo_root: Path,
    log_text: str,
    max_file_chars: int,
) -> str:
    sections: list[str] = []
    for path in files:
        text = (repo_root / path).read_text(encoding="utf-8")
        if len(text) > max_file_chars:
            text = text[:max_file_chars] + "\n# ...truncated..."
        sections.append(f"### FILE: {path}\n```python\n{text}\n```")

    return (
        "You are fixing Python lint/type errors in a repo.\n"
        "Return ONLY a unified diff patch (git-style) for the files provided.\n"
        "Rules:\n"
        "- Only edit listed files.\n"
        "- Keep behavior unchanged unless required to fix errors.\n"
        "- No prose. No markdown fences.\n\n"
        f"## Check Output\n{log_text}\n\n"
        "## Files\n"
        + "\n\n".join(sections)
    )


def call_openai(api_key: str, model: str, prompt: str, base_url: str) -> str:
    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return only valid unified diff."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["choices"][0]["message"]["content"]


def extract_patch(text: str) -> str:
    fence = re.search(r"```(?:diff)?\n(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    patch_start = re.search(r"(?m)^(diff --git |--- )", text)
    if patch_start:
        text = text[patch_start.start() :]
    return text.strip() + "\n"


def write_patch(patch_text: str, patch_file: Path) -> None:
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch_text, encoding="utf-8")


def patch_check(repo_root: Path, patch_file: Path) -> tuple[bool, str]:
    check = subprocess.run(
        ["git", "apply", "--check", "--recount", str(patch_file)],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return check.returncode == 0, check.stdout


def apply_patch(repo_root: Path, patch_file: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"\n[dry-run] Wrote patch to {patch_file}")
        return

    ok, out = patch_check(repo_root, patch_file)
    if not ok:
        raise RuntimeError(f"Patch failed check:\n{out}")

    apply = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "--recount", str(patch_file)],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if apply.returncode != 0:
        raise RuntimeError(f"Patch failed apply:\n{apply.stdout}")


def build_repair_prompt(
    files: list[Path],
    repo_root: Path,
    log_text: str,
    bad_patch: str,
    patch_error: str,
    max_file_chars: int,
) -> str:
    base = build_prompt(files, repo_root, log_text, max_file_chars)
    return (
        base
        + "\n\n## Invalid Patch To Repair\n"
        + bad_patch
        + "\n\n## git apply error\n"
        + patch_error
        + "\n\nReturn a corrected unified diff only. "
        "Do not include explanations or markdown."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run checks and ask OpenAI for first-pass fixes.")
    parser.add_argument("--check-cmd", default="make check")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--max-files", type=int, default=1)
    parser.add_argument("--max-file-chars", type=int, default=12000)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--log-file", default=".run/check.log")
    parser.add_argument("--patch-file", default=".run/ai_fix.patch")
    parser.add_argument("--patch-retries", type=int, default=2)
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is required (env var or .env).", file=sys.stderr)
        return 2

    log_file = (repo_root / args.log_file).resolve()
    patch_file = (repo_root / args.patch_file).resolve()

    for i in range(1, args.iterations + 1):
        print(f"\n=== ai-fix iteration {i}/{args.iterations} ===")
        code = run_check(repo_root, args.check_cmd, log_file)
        if code == 0:
            print("\nChecks passed.")
            return 0

        log_text = log_file.read_text(encoding="utf-8")
        files = parse_error_files(log_text, repo_root)[: args.max_files]
        if not files:
            print("No error files found in check output; nothing to patch.", file=sys.stderr)
            return 1

        print("Target files:")
        for path in files:
            print(f"- {path}")

        prompt = build_prompt(files, repo_root, log_text, args.max_file_chars)
        raw = call_openai(api_key, args.model, prompt, args.base_url)
        patch_text = extract_patch(raw)
        if not patch_text.strip() or patch_text.strip() == "---":
            print("Model returned no usable patch.", file=sys.stderr)
            return 1

        write_patch(patch_text, patch_file)
        if args.dry_run:
            apply_patch(repo_root, patch_file, True)
            return 0

        ok, patch_err = patch_check(repo_root, patch_file)
        retries = 0
        while not ok and retries < args.patch_retries:
            retries += 1
            print(
                f"Patch invalid (attempt {retries}/{args.patch_retries}). "
                "Requesting repaired patch..."
            )
            repair_prompt = build_repair_prompt(
                files,
                repo_root,
                log_text,
                patch_text,
                patch_err,
                args.max_file_chars,
            )
            raw = call_openai(api_key, args.model, repair_prompt, args.base_url)
            patch_text = extract_patch(raw)
            write_patch(patch_text, patch_file)
            ok, patch_err = patch_check(repo_root, patch_file)

        if not ok:
            raise RuntimeError(
                f"Patch failed check:\n{patch_err}\n\n"
                "Tip: retry with smaller scope, e.g. --max-files 1 and more iterations."
            )

        apply_patch(repo_root, patch_file, False)
        print(f"Applied patch: {patch_file}")


    final = run_check(repo_root, args.check_cmd, log_file)
    return final


if __name__ == "__main__":
    raise SystemExit(main())
