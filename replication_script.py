#!/usr/bin/env python3
"""
replication_script.py
=====================

Replication script for the two-stage, RAG-augmented CTCAE v5.0 triage pipeline.

Pipeline (per patient report)
-----------------------------
1. TERM EXTRACTION  - An LLM maps the free-text Spanish patient report to a
   single official CTCAE v5.0 term (English).
2. RAG RETRIEVAL    - The extracted term is looked up in the official NCI
   CTCAE v5.0 reference table; its Grade 1-5 definitions are injected into the
   system prompt as the sole grading authority.
3. GRADE EVALUATION - A second LLM call, constrained to JSON output, assigns a
   grade (or defers to clinical evaluation when the term cannot be graded from
   a patient narrative alone).
4. CONCORDANCE      - The AI grade is compared with the clinician-assigned gold
   standard (Matched / Deferred).

Input  : dataset_50_simulated_cases.json (JSON array of objects).
Output : replication_results.json (JSON array; the input file is never modified).

Usage
-----
    export OPENAI_API_KEY="sk-..."
    python replication_script.py \
        --cases dataset_50_simulated_cases.json \
        --ctcae-xlsx CTCAE_v5.0_MedDRA_v20.1.xlsx \
        --system-prompt system_prompt_ctcae_v5_triage.md \
        --output replication_results.json

Note: the prompts below are intentionally kept in Spanish, exactly as used in
the original experiment, because the patient reports are written in Spanish.
Changing their language would alter the experimental conditions.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

import pandas as pd
from openai import OpenAI

# --------------------------------------------------------------------------- #
# Configuration (identical to the original experiment)
# --------------------------------------------------------------------------- #
MODEL_NAME = "gpt-4o"
TEMPERATURE = 0.0  # Deterministic decoding to maximise reproducibility.

CTCAE_SHEET_NAME = "CTCAE v5.0 Clean Copy"
CTCAE_TERM_COLUMN = "CTCAE Term/MedDRA LLT v20.1"
GRADE_COLUMNS = ["Grade 1", "Grade 2", "Grade 3", "Grade 4", "Grade 5"]
UNDEFINED_LABEL = "No definido"  # Spanish label kept: it is part of the prompt.

# Stage-1 user prompt: forces a bare CTCAE term as the answer.
EXTRACTION_PROMPT_TEMPLATE = (
    "Lee el siguiente reporte: '{report}'. Responde ÚNICAMENTE con el término "
    "oficial CTCAE v5.0 en inglés exacto. Ejemplo: Fatigue"
)

# Stage-2 user prompt: the patient report to be graded.
EVALUATION_PROMPT_TEMPLATE = (
    "Analiza este síntoma reportado por el paciente: {report}"
)

# Closing instruction appended to the retrieved CTCAE definitions.
RAG_STRICT_INSTRUCTION = (
    "Instrucción estricta: Basa la asignación del grado EXCLUSIVAMENTE en "
    "estas definiciones."
)


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def create_client() -> OpenAI:
    """Create an OpenAI client using the OPENAI_API_KEY environment variable.

    The key is never hard-coded; the script aborts if it is not set.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("ERROR: environment variable OPENAI_API_KEY is not set.")
    return OpenAI(api_key=api_key)


def load_cases(path: str) -> List[Dict[str, Any]]:
    """Load the clinical dataset (JSON array of case objects)."""
    with open(path, "r", encoding="utf-8") as fh:
        cases = json.load(fh)
    if not isinstance(cases, list):
        sys.exit(f"ERROR: {path} must contain a JSON array.")
    return cases


def load_ctcae_reference(xlsx_path: str) -> pd.DataFrame:
    """Load the official NCI CTCAE v5.0 reference table (Excel, third-party file).

    Column names are stripped and non-breaking spaces removed, as in the
    original experiment.
    """
    df = pd.read_excel(xlsx_path, sheet_name=CTCAE_SHEET_NAME)
    df.columns = df.columns.str.strip().str.replace("\xa0", "")
    return df


def load_system_prompt(path: str) -> str:
    """Read the triage system prompt (Markdown) from disk."""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def save_results(results: List[Dict[str, Any]], path: str) -> None:
    """Write the results to a JSON file (UTF-8, human readable)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Stage 1: term extraction
# --------------------------------------------------------------------------- #
def extract_ctcae_term(client: OpenAI, report: str) -> str:
    """Map a free-text Spanish report to one official CTCAE v5.0 term."""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "user",
                "content": EXTRACTION_PROMPT_TEMPLATE.format(report=report),
            }
        ],
    )
    return response.choices[0].message.content.strip()


# --------------------------------------------------------------------------- #
# Stage 2: RAG context construction
# --------------------------------------------------------------------------- #
def _format_grade_definition(row: pd.Series, column: str) -> str:
    """Return a single-line grade definition, or a placeholder if missing."""
    value = row.get(column)
    if pd.isna(value):
        return UNDEFINED_LABEL
    return str(value).replace("\n", " ")


def build_rag_context(df_ctcae: pd.DataFrame, term: str) -> str:
    """Retrieve the CTCAE grade definitions for `term` and format them for injection.

    Matching is an exact, case-insensitive comparison on the CTCAE term column.
    If the term is absent from the reference table, a 'no rules found' notice is
    returned instead (the model is then expected to defer).
    """
    matches = df_ctcae[
        df_ctcae[CTCAE_TERM_COLUMN].str.lower().str.strip() == term.lower()
    ]

    if matches.empty:
        return f"\n\n[CONTEXTO RAG INYECTADO]\nNo se encontraron reglas para '{term}'."

    row = matches.iloc[0]
    g1, g2, g3, g4, g5 = (_format_grade_definition(row, c) for c in GRADE_COLUMNS)
    return (
        "\n\n[CONTEXTO RAG INYECTADO - REGLAS OFICIALES]\n"
        f"Término: {row.get(CTCAE_TERM_COLUMN, term)}\n"
        f"- Grado 1: {g1}\n- Grado 2: {g2}\n- Grado 3: {g3}\n"
        f"- Grado 4: {g4}\n- Grado 5: {g5}\n"
        f"{RAG_STRICT_INSTRUCTION}"
    )


# --------------------------------------------------------------------------- #
# Stage 3: RAG-grounded grade evaluation
# --------------------------------------------------------------------------- #
def evaluate_with_rag(
    client: OpenAI, system_prompt: str, rag_context: str, report: str
) -> str:
    """Grade the report using the system prompt plus the injected CTCAE rules.

    Returns the raw JSON string produced by the model.
    """
    response = client.chat.completions.create(
        model=MODEL_NAME,
        temperature=TEMPERATURE,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt + rag_context},
            {
                "role": "user",
                "content": EVALUATION_PROMPT_TEMPLATE.format(report=report),
            },
        ],
    )
    return response.choices[0].message.content


# --------------------------------------------------------------------------- #
# Post-processing
# --------------------------------------------------------------------------- #
def parse_assigned_grade(raw_json: str, term: str) -> Optional[int]:
    """Extract the AI-assigned grade (1-5) from the model's JSON output.

    The adverse event whose CTCAE term equals the extracted term is preferred;
    otherwise the first event is used. Returns None when the model deferred
    (null grade) or the output could not be parsed.
    """
    try:
        events = json.loads(raw_json).get("eventos_adversos", [])
    except (json.JSONDecodeError, AttributeError):
        return None
    if not events:
        return None

    chosen = next(
        (e for e in events
         if str(e.get("termino_ctcae", "")).strip().lower() == term.lower()),
        events[0],
    )
    grade = chosen.get("grado")
    return grade if isinstance(grade, int) and 1 <= grade <= 5 else None


def classify_concordance(gold: Optional[int], ai: Optional[int]) -> str:
    """Return 'Matched' (equal grades), 'Deferred' (no AI grade) or 'Mismatched'.

    'Mismatched' (both graded but different) did not occur in the published
    experiment but is reported explicitly so that it can never be silently
    mislabelled on re-runs.
    """
    if ai is None:
        return "Deferred"
    return "Matched" if ai == gold else "Mismatched"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def process_case(
    client: OpenAI, df_ctcae: pd.DataFrame, system_prompt: str, case: Dict[str, Any]
) -> Dict[str, Any]:
    """Run the full pipeline for one case and return a result record."""
    report = case["Patient_Report_Spanish"]

    term = extract_ctcae_term(client, report)                      # Stage 1
    rag_context = build_rag_context(df_ctcae, term)                # Stage 2
    raw_json = evaluate_with_rag(client, system_prompt, rag_context, report)  # Stage 3

    ai_grade = parse_assigned_grade(raw_json, term)
    gold = case.get("Gold_Standard_CTCAE_Grade")
    return {
        "Case_ID": case["Case_ID"],
        "Extracted_CTCAE_Term": term,
        "AI_Assigned_Grade": ai_grade,
        "Gold_Standard_CTCAE_Grade": gold,
        "Concordance": classify_concordance(gold, ai_grade),
        "AI_Raw_JSON": raw_json,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--cases", default="dataset_50_simulated_cases.json",
                        help="Input JSON dataset of clinical cases.")
    parser.add_argument("--ctcae-xlsx", default="CTCAE_v5.0_MedDRA_v20.1.xlsx",
                        help="Official NCI CTCAE v5.0 reference workbook.")
    parser.add_argument("--system-prompt", default="system_prompt_ctcae_v5_triage.md",
                        help="Markdown file with the triage system prompt.")
    parser.add_argument("--output", default="replication_results.json",
                        help="Output JSON file (input is never overwritten).")
    parser.add_argument("--case-id", action="append", default=None,
                        help="Process only this Case_ID (repeatable), e.g. SP-24.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = create_client()
    cases = load_cases(args.cases)
    df_ctcae = load_ctcae_reference(args.ctcae_xlsx)
    system_prompt = load_system_prompt(args.system_prompt)

    if args.case_id:
        cases = [c for c in cases if c["Case_ID"] in set(args.case_id)]
        if not cases:
            sys.exit("ERROR: none of the requested --case-id values were found.")

    results = []
    for case in cases:
        try:
            record = process_case(client, df_ctcae, system_prompt, case)
            print(f"[OK]    {record['Case_ID']}: term='{record['Extracted_CTCAE_Term']}' "
                  f"AI={record['AI_Assigned_Grade']} gold={record['Gold_Standard_CTCAE_Grade']} "
                  f"-> {record['Concordance']}")
            results.append(record)
        except Exception as exc:  # keep going so one API failure does not lose the run
            print(f"[ERROR] {case.get('Case_ID')}: {exc}")
            results.append({"Case_ID": case.get("Case_ID"), "Error": str(exc)})
        save_results(results, args.output)  # checkpoint after every case

    ok = [r for r in results if "Error" not in r]
    matched = sum(r["Concordance"] == "Matched" for r in ok)
    print(f"\nDone. {len(ok)}/{len(cases)} processed, {matched} matched. "
          f"Results written to {args.output}")


if __name__ == "__main__":
    main()
