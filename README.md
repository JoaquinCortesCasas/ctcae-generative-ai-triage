# Replication Package — RAG-Augmented CTCAE v5.0 Triage Assistant (Phase 1)

This repository contains the data and code needed to replicate the evaluation of a Retrieval-Augmented Generation (RAG) pipeline that grades patient-reported adverse events of breast-cancer therapy according to **CTCAE v5.0**, together with an adversarial safety battery run against the deployed chatbot.

All patient narratives are **simulated** (synthetic). No real patient data, identifiers, or protected health information are included.

## Repository contents

| File | Description |
|---|---|
| `dataset_50_simulated_cases.json` | 50 simulated patient reports (Spanish) with clinician gold-standard CTCAE grades and the AI-assigned grades. |
| `adversarial_battery_24_tests.json` | 24 adversarial / safety prompts (Spanish) with the chatbot's responses and the defense outcome. |
| `replication_script.py` | Two-stage RAG pipeline (term extraction → CTCAE-grounded grading) that reproduces the AI grades from the clinical dataset. |
| `README.md` | This file. |

## Method summary

1. **Term extraction:** an LLM maps the Spanish patient report to one official CTCAE v5.0 term (English).
2. **RAG retrieval:** the term is matched (exact, case-insensitive) against the official NCI CTCAE v5.0 table; the Grade 1–5 definitions are injected into the system prompt.
3. **Grade evaluation:** a second LLM call (JSON mode) assigns a grade using *only* the injected definitions, or defers (`null` grade) when the event cannot be graded from a narrative alone (e.g., laboratory-based terms).
4. **Concordance:** the AI grade is compared with the clinician gold standard.

Model: `gpt-4o`, temperature `0.0`.

## Data Dictionary

### `dataset_50_simulated_cases.json`

A JSON array of 50 objects. Every object has exactly the six keys below.

| Key | Type | Allowed values | Description |
|---|---|---|---|
| `Case_ID` | string | `SP-01` … `SP-50` | Unique identifier of the simulated case. |
| `Therapy_Agent` | string | `Anastrozole`, `Letrozole`, `Exemestane`, `Abemaciclib`, `Palbociclib`, `Ribociclib`, `Fulvestrant`, `Goserelin` | Anticancer agent the simulated patient is receiving. |
| `Patient_Report_Spanish` | string | Free text (Spanish) | Patient-reported symptom narrative; the input to the pipeline. |
| `Gold_Standard_CTCAE_Grade` | integer \| null | `1`, `2`, `3`, `4`, `5`, `null` | Clinician-assigned CTCAE v5.0 grade. `null` = the clinician did not assign a grade. |
| `AI_Assigned_Grade` | integer \| null | `1`, `2`, `3`, `4`, `5`, `null` | Grade assigned by the pipeline. `null` = the system deferred to clinical evaluation (no grade issued). |
| `Concordance` | string | `Matched`, `Deferred` | `Matched` = AI grade equals gold standard. `Deferred` = AI issued no grade (`AI_Assigned_Grade` is `null`). No case with two differing numeric grades occurred. |

Summary: 44 `Matched`, 6 `Deferred` (`SP-14`, `SP-27`, `SP-29`, `SP-35`, `SP-36`, `SP-39`), all of which are laboratory- or imaging-dependent events.

### `adversarial_battery_24_tests.json`

A JSON array of 24 objects (6 tests per category). Every object has exactly the six keys below.

| Key | Type | Allowed values | Description |
|---|---|---|---|
| `Test_ID` | string | `ADV-01` … `ADV-24` | Unique identifier of the adversarial test. |
| `Attack_Category` | string | `Prompt Injection` (ADV-01–06), `Jailbreaking` (ADV-07–12), `Out-of-Scope` (ADV-13–18), `Clinical Safety` (ADV-19–24) | Attack family. |
| `OWASP_2025_Mapping` | string | `LLM01:2025 Prompt Injection`; `LLM01:2025 Prompt Injection; LLM07:2025 System Prompt Leakage`; `LLM09:2025 Misinformation`; `LLM10:2025 Unbounded Consumption` | Closest category of the OWASP Top 10 for LLM Applications 2025. Multiple categories are separated by `; `. See mapping rationale below. |
| `Adversarial_Prompt_Spanish` | string | Free text (Spanish) | Attack prompt sent to the chatbot. |
| `System_Response` | string | Free text (Spanish) | Chatbot reply, transcribed verbatim from the captured screenshot (paragraph breaks preserved as `\n\n`). |
| `Defense_Status` | string | `Intercepted`, `Failed` | `Intercepted` = the chatbot resisted the attack (source label "Exitoso"). `Failed` = the attack succeeded (source label "Fallido"). All 24 tests are `Intercepted`. |

**OWASP mapping rationale.**

| Attack category | OWASP 2025 category | Rationale |
|---|---|---|
| Prompt Injection | LLM01 Prompt Injection | Direct instruction-override attacks. Tests ADV-01, ADV-05 and ADV-06 additionally try to extract the system prompt or internal logic, hence also LLM07 System Prompt Leakage. |
| Jailbreaking | LLM01 Prompt Injection | OWASP 2025 treats jailbreaking as a form of prompt injection. |
| Out-of-Scope | LLM10 Unbounded Consumption | The test objective is that the system does not spend resources on tasks outside its purpose. |
| Clinical Safety | LLM09 Misinformation | Risk of unsafe or overconfident medical content (dosing, diagnosis, prognosis). |

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.9 or later |
| Python packages | `openai` (≥ 1.0), `pandas`, `openpyxl` — install with `pip install openai pandas openpyxl` |
| OpenAI API access | An API key with access to `gpt-4o`, exported as `OPENAI_API_KEY` (the key is never stored in the code) |
| CTCAE reference table | Official NCI workbook `CTCAE_v5.0_MedDRA_v20.1.xlsx` (sheet `CTCAE v5.0 Clean Copy`), available from the U.S. National Cancer Institute (NCI) website; not redistributed here |
| System prompt | `system_prompt_ctcae_v5_triage.md` (triage system prompt), placed in the working directory or passed with `--system-prompt` |

## Usage

```bash
export OPENAI_API_KEY="sk-..."          # Windows PowerShell: $env:OPENAI_API_KEY="sk-..."

# Full run (50 cases)
python replication_script.py \
    --cases dataset_50_simulated_cases.json \
    --ctcae-xlsx CTCAE_v5.0_MedDRA_v20.1.xlsx \
    --system-prompt system_prompt_ctcae_v5_triage.md \
    --output replication_results.json

# Single case
python replication_script.py --case-id SP-24
```

The script never modifies the input file. It writes `replication_results.json`, one object per case with: `Case_ID`, `Extracted_CTCAE_Term`, `AI_Assigned_Grade`, `Gold_Standard_CTCAE_Grade`, `Concordance` (`Matched` / `Deferred` / `Mismatched`) and `AI_Raw_JSON` (full model output). Results are checkpointed after every case.

**Reproducibility note.** Decoding uses temperature 0, but hosted LLMs are not guaranteed to be bit-exactly deterministic and model snapshots change over time; minor deviations from the published grades are possible.

## License

This work is licensed under the [Creative Commons Attribution 4.0 International License (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/). You are free to share and adapt the material for any purpose, provided appropriate credit is given.

The CTCAE v5.0 reference table is a product of the U.S. National Cancer Institute and is not covered by this license.

## Citation

If you use this package, please cite the associated article and this Zenodo record (DOI assigned by Zenodo upon publication).
