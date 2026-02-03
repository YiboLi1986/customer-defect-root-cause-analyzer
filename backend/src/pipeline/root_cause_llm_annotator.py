import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

import json
from typing import Dict, Any, Iterable, Optional, List

from backend.src.data_io.file_reader import FileReader
from backend.src.data_io.file_writer import FileWriter
from backend.src.llm.copilot_client import CopilotClient


class RootCauseLLMAnnotator:
    """
    Read case JSONL -> for each case build (system+user) prompts -> call LLM ->
    write root_cause / root_cause_type / root_cause_subtype into a COPY of the case ->
    stream-write to a new JSONL file (never overwrite the original).
    """

    def __init__(
        self,
        input_jsonl: str,
        output_jsonl: str,
        system_prompt_path: str,
        user_prompt_path: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        limit: Optional[int] = None,
        # Optional preprocessing controls (to avoid very long single-case inputs)
        enable_preprocess: bool = True,
        max_comments: int = 15,
        max_comment_chars: int = 800,
        max_repro_steps: int = 5,
        max_repro_chars: int = 1200,
    ) -> None:
        """
        Args:
            input_jsonl: Path to cases.jsonl produced by BuildCaseJson.
            output_jsonl: Path to write annotated cases (new file).
            system_prompt_path: Path to system prompt text file.
            user_prompt_path: Path to user prompt text file (must contain {case_json} placeholder).
            model: Optional model override for CopilotClient.
            temperature: LLM temperature.
            limit: If set, only process first N cases; if None, process all.
            enable_preprocess: If True, truncate long comments/repro_steps before sending to LLM.
            max_comments: Max number of comment entries sent to LLM.
            max_comment_chars: Max chars per comment text sent to LLM.
            max_repro_steps: Max number of repro step strings sent to LLM.
            max_repro_chars: Max chars per repro step sent to LLM.
        """
        self.input_jsonl = input_jsonl
        self.output_jsonl = output_jsonl

        self.system_prompt = FileReader.read_text(system_prompt_path)
        self.user_prompt_template = FileReader.read_text(user_prompt_path)

        self.limit = limit

        self.enable_preprocess = enable_preprocess
        self.max_comments = int(max_comments)
        self.max_comment_chars = int(max_comment_chars)
        self.max_repro_steps = int(max_repro_steps)
        self.max_repro_chars = int(max_repro_chars)

        self.client = CopilotClient(
            model=model,
            temperature=temperature,
        )

    # ---------------- Public API ----------------

    def run(self) -> str:
        """
        Execute LLM annotation.

        Returns:
            Output JSONL path.
        """
        records = self._iter_input_cases()
        annotated_records = self._annotate_records(records)
        FileWriter.write_jsonl(annotated_records, self.output_jsonl, ensure_ascii=False)
        return self.output_jsonl

    # ---------------- Internals ----------------

    def _iter_input_cases(self) -> Iterable[Dict[str, Any]]:
        """
        Stream input JSONL cases. Honors self.limit.
        """
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
        For each case, call LLM and merge result into a copied record.
        Fail-safe: if a case fails, keep it and attach _llm_error.
        """
        for case in records:
            annotated = dict(case)  # copy (do not overwrite input object)

            try:
                llm_result = self._call_llm(case)
                self._merge_llm_result(annotated, llm_result)
            except Exception as e:
                # Keep original values, attach error for debugging
                annotated.setdefault("root_cause", case.get("root_cause"))
                annotated.setdefault("root_cause_type", case.get("root_cause_type"))
                annotated.setdefault("root_cause_subtype", case.get("root_cause_subtype"))
                annotated["_llm_error"] = str(e)

            yield annotated

    def _call_llm(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build prompts for ONE case and call LLM. Expect JSON output with 3 keys.
        """
        case_for_llm = self._prepare_case_for_llm(case) if self.enable_preprocess else case
        case_json = json.dumps(case_for_llm, ensure_ascii=False)

        # Build user prompt with placeholder
        user_prompt = CopilotClient.build_user_prompt(
            self.user_prompt_template,
            case_json=case_json,
        )

        text = self.client.chat_text(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
        )

        return self._parse_llm_json(text)

    def _prepare_case_for_llm(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """
        Truncate potentially long fields before sending to LLM.
        - Keeps all 'fields' intact.
        - Trims comments and repro_steps to control token usage.
        """
        new_case: Dict[str, Any] = dict(case)

        # -------- comments --------
        comments = case.get("comment", [])
        if isinstance(comments, list):
            trimmed: List[Dict[str, Any]] = []
            # Keep last N comments by default (simple + predictable)
            # If you want keyword-based filtering later, swap logic here.
            for c in comments[-self.max_comments:]:
                if not isinstance(c, dict):
                    continue
                c2 = dict(c)
                txt = c2.get("text")
                if isinstance(txt, str) and len(txt) > self.max_comment_chars:
                    c2["text"] = txt[:self.max_comment_chars]
                trimmed.append(c2)
            new_case["comment"] = trimmed
        else:
            new_case["comment"] = []

        # -------- repro_steps --------
        repro_steps = case.get("repro_steps", [])
        if isinstance(repro_steps, list):
            rep_trimmed: List[str] = []
            for r in repro_steps[:self.max_repro_steps]:
                if r is None:
                    continue
                s = str(r)
                if len(s) > self.max_repro_chars:
                    s = s[:self.max_repro_chars]
                rep_trimmed.append(s)
            new_case["repro_steps"] = rep_trimmed
        else:
            new_case["repro_steps"] = []

        return new_case

    @staticmethod
    def _parse_llm_json(text: str) -> Dict[str, Any]:
        """
        Parse and minimally validate LLM JSON output.
        Ensures the 3 required keys exist; missing keys become "".
        """
        # Some models may accidentally wrap JSON with whitespace; strip first.
        raw = text.strip()

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # Very common failure mode: extra text around JSON.
            # We keep it strict here; you can relax later if needed.
            raise ValueError(f"LLM output is not valid JSON. First 300 chars: {raw[:300]}")

        # Ensure required keys exist
        for k in ("root_cause", "root_cause_type", "root_cause_subtype"):
            if k not in obj:
                obj[k] = ""

        # Normalize None -> "" (optional, but helps downstream)
        for k in ("root_cause", "root_cause_type", "root_cause_subtype"):
            if obj[k] is None:
                obj[k] = ""

        return obj

    @staticmethod
    def _merge_llm_result(case: Dict[str, Any], llm_result: Dict[str, Any]) -> None:
        """
        Merge LLM output into case dict (in-place).
        """
        case["root_cause"] = llm_result.get("root_cause", "")
        case["root_cause_type"] = llm_result.get("root_cause_type", "")
        case["root_cause_subtype"] = llm_result.get("root_cause_subtype", "")


if __name__ == "__main__":
    INPUT_JSONL = "backend/src/output/cases.jsonl"

    OUTPUT_JSONL = "backend/src/output/cases_with_root_cause.jsonl"
    
    SYSTEM_PROMPT_PATH = "backend/src/prompt/root_cause.system.txt"
    USER_PROMPT_PATH = "backend/src/prompt/root_cause.user.txt"

    annotator = RootCauseLLMAnnotator(
        input_jsonl=INPUT_JSONL,
        output_jsonl=OUTPUT_JSONL,
        system_prompt_path=SYSTEM_PROMPT_PATH,
        user_prompt_path=USER_PROMPT_PATH,
        temperature=0.2,
        limit=10,  # set to None for full run
        enable_preprocess=True,  # set False if you want full raw case input
        max_comments=15,
        max_comment_chars=800,
        max_repro_steps=5,
        max_repro_chars=1200,
    )

    out_path = annotator.run()
    print(f"[RootCauseLLMAnnotator] Wrote: {out_path}")
