import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

import json
import re
from typing import Dict, Any, Iterable, Optional, List

from backend.src.data_io.file_reader import FileReader
from backend.src.data_io.file_writer import FileWriter
from backend.src.llm.copilot_client import CopilotClient


class RootCauseLLMAnnotatorV2StateAware:
    """
    V2 (RootCause only) Strategy:

    - Input: cases_v2.jsonl (built by BuildCaseJsonV2WithNotes), each record retains full original fields:
        {
          "work_item_id": ...,
          "fields": {...},
          "comment": [...],
          "repro_steps": [...],
          "additional_notes": [...],   # raw strings containing state blocks
          "root_cause": null/""/existing,
          ...
        }

    - Internal LLM input (only when evidence exists):
        We ONLY feed:
          - fields
          - repro_steps (trimmed)
          - evidence.notes_used (ONLY state == Assigned or Investigating)

        We DO NOT feed comments in V2 to reduce noise.

    - Decision:
        A) If Assigned/Investigating evidence exists -> call LLM
        B) If no such evidence -> B2: skip LLM, DO NOT call model
           (we keep existing root_cause if present; otherwise set to "").

    - Output: a COPY of each original record, with only "root_cause" filled (others unchanged),
      plus optional debug fields: _evidence_status, _evidence_states_found, _evidence_states_used, etc.
    """

    # More robust than "(update)" hard-coding:
    # Allows optional "(...)" metadata between "on <time>" and "===="
    STATE_BLOCK_RE = re.compile(
        r"====\s*State:\s*(?P<state>[^=]+?)\s*by:\s*(?P<by>.+?)\s*on\s*(?P<on>.+?)\s*(\([^)]+\))?\s*====\s*(?P<body>.*?)(?=(====\s*State:)|\Z)",
        re.DOTALL | re.IGNORECASE,
    )

    def __init__(
        self,
        input_jsonl: str,
        output_jsonl: str,
        system_prompt_path: str,
        user_prompt_path: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        limit: Optional[int] = None,
        # Input tightening / token control
        max_state_blocks: int = 8,          # keep last N Assigned/Investigating blocks
        max_state_block_chars: int = 2500,  # truncate each block body
        max_repro_steps: int = 5,
        max_repro_chars: int = 1200,
        # Debug / traceability
        attach_evidence_debug: bool = True,
    ) -> None:
        self.input_jsonl = input_jsonl
        self.output_jsonl = output_jsonl

        self.system_prompt = FileReader.read_text(system_prompt_path)
        self.user_prompt_template = FileReader.read_text(user_prompt_path)

        self.limit = limit

        self.max_state_blocks = int(max_state_blocks)
        self.max_state_block_chars = int(max_state_block_chars)
        self.max_repro_steps = int(max_repro_steps)
        self.max_repro_chars = int(max_repro_chars)

        self.attach_evidence_debug = bool(attach_evidence_debug)

        self.client = CopilotClient(
            model=model,
            temperature=temperature,
        )

    # ---------------- Public API ----------------

    def run(self) -> str:
        records = self._iter_input_cases()
        annotated_records = self._annotate_records(records)
        FileWriter.write_jsonl(annotated_records, self.output_jsonl, ensure_ascii=False)
        return self.output_jsonl

    # ---------------- Internals ----------------

    def _iter_input_cases(self) -> Iterable[Dict[str, Any]]:
        with open(self.input_jsonl, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if self.limit is not None and idx >= self.limit:
                    break
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    def _annotate_records(self, records: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
        """
        Stream-processing: for each case, decide A/B2, optionally call LLM,
        then output a copied record with root_cause filled.
        """
        for case in records:
            annotated = dict(case)  # copy (do not overwrite input object)

            try:
                evidence = self._build_state_evidence(case)
                has_evidence = evidence["has_state_evidence"]

                # Always attach evidence status for traceability
                annotated["_evidence_status"] = (
                    "has_assigned_or_investigating" if has_evidence else "no_assigned_or_investigating"
                )

                if self.attach_evidence_debug:
                    annotated["_evidence_states_found"] = evidence.get("all_states_found", [])
                    annotated["_evidence_states_used"] = evidence.get("states_used", [])
                    annotated["_evidence_blocks_used_count"] = len(evidence.get("notes_used", []))

                # -------- B2: no evidence -> skip LLM --------
                if not has_evidence:
                    # Do NOT overwrite an existing root_cause if one is already present.
                    if not annotated.get("root_cause"):
                        annotated["root_cause"] = ""
                    yield annotated
                    continue

                # -------- A: evidence exists -> call LLM --------
                llm_result = self._call_llm(case, evidence)
                self._merge_llm_result(annotated, llm_result)

            except Exception as e:
                # Fail-safe: keep original record but attach error
                annotated.setdefault("root_cause", case.get("root_cause"))
                annotated["_llm_error"] = str(e)

            yield annotated

    # ---------------- Evidence extraction ----------------

    def _build_state_evidence(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse additional_notes and pick only Assigned/Investigating blocks.
        """
        notes = case.get("additional_notes", [])
        chunks = self._extract_state_chunks(notes)

        all_states = sorted(list({c["state"] for c in chunks})) if chunks else []
        selected = [c for c in chunks if c.get("state") in ("Assigned", "Investigating")]

        # keep last N blocks (usually most useful)
        if len(selected) > self.max_state_blocks:
            selected = selected[-self.max_state_blocks :]

        # truncate each block
        for c in selected:
            txt = c.get("text", "")
            if isinstance(txt, str) and len(txt) > self.max_state_block_chars:
                c["text"] = txt[: self.max_state_block_chars]

        states_used = sorted(list({c["state"] for c in selected})) if selected else []

        return {
            "has_state_evidence": len(selected) > 0,
            "states_used": states_used,
            "notes_used": selected,  # structured list[dict]
            "all_states_found": all_states,
        }

    def _extract_state_chunks(self, additional_notes: Any) -> List[Dict[str, str]]:
        chunks: List[Dict[str, str]] = []
        if not isinstance(additional_notes, list):
            return chunks

        for note in additional_notes:
            if not isinstance(note, str) or not note.strip():
                continue

            for m in self.STATE_BLOCK_RE.finditer(note):
                state_raw = (m.group("state") or "").strip()
                by = (m.group("by") or "").strip()
                on = (m.group("on") or "").strip()
                body = (m.group("body") or "").strip()

                s = state_raw.lower()
                if "assign" in s:
                    state = "Assigned"
                elif "investig" in s:
                    state = "Investigating"
                else:
                    state = state_raw  # keep as-is

                chunks.append({
                    "state": state,
                    "by": by,
                    "on": on,
                    "text": body,
                })

        return chunks

    # ---------------- LLM call ----------------

    def _call_llm(self, case: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build prompts for ONE case and call LLM. Expect JSON output with ONE key: root_cause.
        """
        case_for_llm = self._prepare_case_for_llm(case, evidence)
        case_json = json.dumps(case_for_llm, ensure_ascii=False)

        user_prompt = CopilotClient.build_user_prompt(
            self.user_prompt_template,
            case_json=case_json,
        )

        text = self.client.chat_text(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
        )

        return self._parse_llm_json(text)

    def _prepare_case_for_llm(self, case: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        LLM input payload (compact + state-aware):
          {
            "work_item_id": ...,
            "fields": {...},
            "repro_steps": [...trimmed...],
            "evidence": {
               "policy": "...",
               "states_used": [...],
               "notes_used": [ {state, by, on, text}, ... ]
            }
          }
        """
        return {
            "work_item_id": case.get("work_item_id"),
            "fields": case.get("fields", {}),
            "repro_steps": self._trim_repro(case.get("repro_steps", [])),
            "evidence": {
                "policy": (
                    "Only note blocks where state == Assigned or Investigating are provided. "
                    "Use them together with repro_steps and fields to infer root cause. "
                    "If evidence is insufficient, return an empty string."
                ),
                "states_used": evidence.get("states_used", []),
                "notes_used": evidence.get("notes_used", []),
            },
        }

    def _trim_repro(self, repro_steps: Any) -> List[str]:
        if not isinstance(repro_steps, list):
            return []
        rep_trimmed: List[str] = []
        for r in repro_steps[: self.max_repro_steps]:
            if r is None:
                continue
            s = str(r)
            if len(s) > self.max_repro_chars:
                s = s[: self.max_repro_chars]
            rep_trimmed.append(s)
        return rep_trimmed

    # ---------------- Parsing + merge ----------------

    @staticmethod
    def _parse_llm_json(text: str) -> Dict[str, Any]:
        """
        Expect JSON with exactly one key: root_cause (string).
        Fail fast if JSON invalid; allow missing key -> "".
        """
        raw = text.strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"LLM output is not valid JSON. First 300 chars: {raw[:300]}")

        if "root_cause" not in obj or obj["root_cause"] is None:
            obj["root_cause"] = ""

        if not isinstance(obj["root_cause"], str):
            obj["root_cause"] = str(obj["root_cause"])

        # Optional strictness:
        # obj = {"root_cause": obj.get("root_cause", "")}

        return obj

    @staticmethod
    def _merge_llm_result(case: Dict[str, Any], llm_result: Dict[str, Any]) -> None:
        case["root_cause"] = llm_result.get("root_cause", "")


if __name__ == "__main__":
    INPUT_JSONL = "backend/src/output/cases_v2.jsonl"
    OUTPUT_JSONL = "backend/src/output/cases_v2_with_root_cause.jsonl"

    SYSTEM_PROMPT_PATH = "backend/src/prompt/root_cause_only_v2.system.txt"
    USER_PROMPT_PATH = "backend/src/prompt/root_cause_only_v2.user.txt"

    annotator = RootCauseLLMAnnotatorV2StateAware(
        input_jsonl=INPUT_JSONL,
        output_jsonl=OUTPUT_JSONL,
        system_prompt_path=SYSTEM_PROMPT_PATH,
        user_prompt_path=USER_PROMPT_PATH,
        temperature=0.2,
        limit=30,  # None for full run
        max_state_blocks=8,
        max_state_block_chars=2500,
        max_repro_steps=5,
        max_repro_chars=1200,
        attach_evidence_debug=True,
    )

    out_path = annotator.run()
    print(f"[RootCauseLLMAnnotatorV2StateAware] Wrote: {out_path}")
