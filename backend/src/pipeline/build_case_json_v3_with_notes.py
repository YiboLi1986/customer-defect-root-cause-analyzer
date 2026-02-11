import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from typing import List, Dict, Any, Iterable, Union, Tuple, Optional
import pandas as pd

from backend.src.data_io.file_reader import FileReader
from backend.src.data_io.file_writer import FileWriter


class BuildCaseJsonV3WithNotes:
    """
    V3: Based on V2, with additional fields added into `fields`:
      - Product Name (from main sheet column `Product` by default)
      - Product Version (from main sheet column `Reported Version` by default; configurable)
      - Software Configuration Management (from main sheet column `SCM` by default)
      - CodeChange (from main sheet column `CodeChange`)
      - Code Change/Non-code Change (from main sheet column `Code Change/Non-code Change`)

    Output schema (per work item):
      {
        "work_item_id": 1551759,
        "fields": {..., "Product Name": ..., "Product Version": ..., "Software Configuration Management": ...,
                   "CodeChange": ..., "Code Change/Non-code Change": ...},
        "comment": [...],
        "repro_steps": [...],
        "additional_notes": [...],
        "root_cause": null,
        "root_cause_type": null,
        "root_cause_subtype": null
      }
    """

    def __init__(
        self,
        xlsx_path: str,
        keep_fields: List[str],
        output_dir: str,
        output_name: str = "cases_v3.jsonl",
        output_format: str = "jsonl",  # "jsonl" or "json"

        # sheet names
        main_sheet: Union[str, int] = " P1P2 Customer Defects",
        comments_sheet: Union[str, int] = "Comments",
        repro_sheet: Union[str, int] = "Repo Steps",
        notes_sheet: Union[str, int] = "Notes",

        # common id col
        work_item_id_col: str = "WorkItemId",

        # sheet2 (comments)
        comment_col: str = "Comment",
        created_col: str = "CreatedDate",
        modified_col: str = "ModifiedDate",
        author_col: str = "AuthorName",

        # sheet3 (repro)
        repro_col: str = "ReproSteps",

        # sheet4 (notes)
        additional_notes_col: str = "AdditionalNotes",

        # main sheet root cause columns (used to fill slots if present)
        root_cause_col: str = "Root Cause",
        root_cause_type_col: str = "Root Cause Type",
        root_cause_subtype_col: str = "Root Cause Subtype",

        # V3 additions (source columns in main sheet)
        product_name_col: str = "Product",
        product_version_col: str = "Reported Version",

        scm_col: str = "SCM",
        code_change_col: str = "CodeChange",
        code_change_class_col: str = "Code Change/Non-code Change",
    ) -> None:
        self.xlsx_path = xlsx_path
        self.keep_fields = keep_fields
        self.output_dir = output_dir
        self.output_name = output_name
        self.output_format = output_format.lower().strip()

        self.main_sheet = main_sheet
        self.comments_sheet = comments_sheet
        self.repro_sheet = repro_sheet
        self.notes_sheet = notes_sheet

        self.work_item_id_col = work_item_id_col

        self.comment_col = comment_col
        self.created_col = created_col
        self.modified_col = modified_col
        self.author_col = author_col

        self.repro_col = repro_col
        self.additional_notes_col = additional_notes_col

        self.root_cause_col = root_cause_col
        self.root_cause_type_col = root_cause_type_col
        self.root_cause_subtype_col = root_cause_subtype_col

        # V3 additions
        self.product_name_col = product_name_col
        self.product_version_col = product_version_col

        self.scm_col = scm_col
        self.code_change_col = code_change_col
        self.code_change_class_col = code_change_class_col

        # Output keys (SCM must be full name)
        self.out_product_name_key = "Product Name"
        self.out_product_version_key = "Product Version"
        self.out_scm_key = "Software Configuration Management"  # full name required

    # ---------------- Public API ----------------

    def run(self) -> str:
        """
        Execute: read sheets -> aggregate -> merge -> write JSONL/JSON.
        Returns: Output file path.
        """
        df_main, df_cmt, df_rep, df_notes = self._read_sheets()
        df_main, df_cmt, df_rep, df_notes = self._normalize_ids(df_main, df_cmt, df_rep, df_notes)

        keep_fields = self._validate_keep_fields(df_main)

        cmt_group = self._aggregate_comments(df_cmt)
        rep_group = self._aggregate_repro_steps(df_rep)
        notes_group = self._aggregate_additional_notes(df_notes)

        df_out = self._merge(df_main, keep_fields, cmt_group, rep_group, notes_group)
        records = self._iter_records(df_out, keep_fields)

        out_path = os.path.join(self.output_dir, self.output_name)
        self._write(records, out_path)
        return out_path

    # ---------------- Internals ----------------

    def _read_sheets(self) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        df_main = FileReader.read_xlsx(self.xlsx_path, sheet_name=self.main_sheet)
        df_cmt = FileReader.read_xlsx(self.xlsx_path, sheet_name=self.comments_sheet)
        df_rep = FileReader.read_xlsx(self.xlsx_path, sheet_name=self.repro_sheet)
        df_notes = FileReader.read_xlsx(self.xlsx_path, sheet_name=self.notes_sheet)
        return df_main, df_cmt, df_rep, df_notes

    def _normalize_ids(
        self,
        df_main: pd.DataFrame,
        df_cmt: pd.DataFrame,
        df_rep: pd.DataFrame,
        df_notes: pd.DataFrame
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        for df, name in [
            (df_main, "main"),
            (df_cmt, "comments"),
            (df_rep, "repro"),
            (df_notes, "notes"),
        ]:
            if self.work_item_id_col not in df.columns:
                raise ValueError(f"Missing '{self.work_item_id_col}' in {name} sheet.")

        # normalize to int64 for stable merges
        df_main[self.work_item_id_col] = df_main[self.work_item_id_col].astype("int64")
        df_cmt[self.work_item_id_col] = df_cmt[self.work_item_id_col].astype("int64")
        df_rep[self.work_item_id_col] = df_rep[self.work_item_id_col].astype("int64")
        df_notes[self.work_item_id_col] = df_notes[self.work_item_id_col].astype("int64")
        return df_main, df_cmt, df_rep, df_notes

    def _validate_keep_fields(self, df_main: pd.DataFrame) -> List[str]:
        missing = [c for c in self.keep_fields if c not in df_main.columns]
        if missing:
            raise ValueError(f"Missing columns in main sheet: {missing}")
        return self.keep_fields

    def _aggregate_comments(self, df_cmt: pd.DataFrame) -> pd.DataFrame:
        if self.comment_col not in df_cmt.columns:
            return pd.DataFrame({self.work_item_id_col: [], "comment": []})

        df = df_cmt.dropna(subset=[self.comment_col]).copy()
        df[self.comment_col] = df[self.comment_col].astype(str)

        sort_cols = [self.work_item_id_col]
        if self.created_col in df.columns:
            sort_cols.append(self.created_col)
        df = df.sort_values(sort_cols, na_position="last")

        def build_comment_list(g: pd.DataFrame) -> List[Dict[str, Any]]:
            items: List[Dict[str, Any]] = []
            for _, r in g.iterrows():
                items.append({
                    "text": self._nan_to_none(r.get(self.comment_col)),
                    "created_date": self._nan_to_none(r.get(self.created_col)),
                    "modified_date": self._nan_to_none(r.get(self.modified_col)),
                    "author": self._nan_to_none(r.get(self.author_col)),
                })
            return items

        grouped = (
            df.groupby(self.work_item_id_col, sort=False)
              .apply(build_comment_list)
              .reset_index(name="comment")
        )
        return grouped

    def _aggregate_repro_steps(self, df_rep: pd.DataFrame) -> pd.DataFrame:
        if self.repro_col not in df_rep.columns:
            return pd.DataFrame({self.work_item_id_col: [], "repro_steps": []})

        df = df_rep.dropna(subset=[self.repro_col]).copy()
        df[self.repro_col] = df[self.repro_col].astype(str)

        grouped = (
            df.groupby(self.work_item_id_col, sort=False)[self.repro_col]
              .apply(list)
              .reset_index(name="repro_steps")
        )
        return grouped

    def _aggregate_additional_notes(self, df_notes: pd.DataFrame) -> pd.DataFrame:
        if self.additional_notes_col not in df_notes.columns:
            return pd.DataFrame({self.work_item_id_col: [], "additional_notes": []})

        df = df_notes.dropna(subset=[self.additional_notes_col]).copy()
        df[self.additional_notes_col] = df[self.additional_notes_col].astype(str)

        grouped = (
            df.groupby(self.work_item_id_col, sort=False)[self.additional_notes_col]
              .apply(list)
              .reset_index(name="additional_notes")
        )
        return grouped

    def _merge(
        self,
        df_main: pd.DataFrame,
        keep_fields: List[str],
        cmt_group: pd.DataFrame,
        rep_group: pd.DataFrame,
        notes_group: pd.DataFrame
    ) -> pd.DataFrame:
        base_cols = [self.work_item_id_col] + keep_fields

        # keep root cause columns if present (to fill slots), without forcing in keep_fields
        extra_cols: List[str] = []
        for col in [self.root_cause_col, self.root_cause_type_col, self.root_cause_subtype_col]:
            if col in df_main.columns:
                extra_cols.append(col)

        # V3 additions: include them in df_out even if not in keep_fields
        for col in [
            self.product_name_col,
            self.product_version_col,
            self.scm_col,
            self.code_change_col,
            self.code_change_class_col,
        ]:
            if col in df_main.columns and col not in base_cols and col not in extra_cols:
                extra_cols.append(col)

        df_out = df_main[base_cols + extra_cols].copy()

        if not cmt_group.empty:
            df_out = df_out.merge(cmt_group, on=self.work_item_id_col, how="left")
        else:
            df_out["comment"] = None

        if not rep_group.empty:
            df_out = df_out.merge(rep_group, on=self.work_item_id_col, how="left")
        else:
            df_out["repro_steps"] = None

        if not notes_group.empty:
            df_out = df_out.merge(notes_group, on=self.work_item_id_col, how="left")
        else:
            df_out["additional_notes"] = None

        df_out["comment"] = df_out["comment"].apply(lambda x: x if isinstance(x, list) else [])
        df_out["repro_steps"] = df_out["repro_steps"].apply(lambda x: x if isinstance(x, list) else [])
        df_out["additional_notes"] = df_out["additional_notes"].apply(lambda x: x if isinstance(x, list) else [])
        return df_out

    def _iter_records(self, df_out: pd.DataFrame, keep_fields: List[str]) -> Iterable[Dict[str, Any]]:
        for _, row in df_out.iterrows():
            # start with the original keep_fields behavior (unchanged)
            fields: Dict[str, Any] = {k: self._nan_to_none(row.get(k)) for k in keep_fields}

            # ---- V3 additions (added into fields, without changing other logic) ----
            # Product name/version (leave None if missing)
            product_name_val = self._nan_to_none(row.get(self.product_name_col)) if self.product_name_col in df_out.columns else None
            product_version_val = self._nan_to_none(row.get(self.product_version_col)) if self.product_version_col in df_out.columns else None

            fields[self.out_product_name_key] = product_name_val
            fields[self.out_product_version_key] = product_version_val

            # SCM (must be full name key)
            scm_val = self._nan_to_none(row.get(self.scm_col)) if self.scm_col in df_out.columns else None
            fields[self.out_scm_key] = scm_val

            # Code change columns (keep original names)
            code_change_val = self._nan_to_none(row.get(self.code_change_col)) if self.code_change_col in df_out.columns else None
            fields[self.code_change_col] = self._to_bool_or_none(code_change_val)

            code_change_class_val = self._nan_to_none(row.get(self.code_change_class_col)) if self.code_change_class_col in df_out.columns else None
            fields[self.code_change_class_col] = code_change_class_val
            # ----------------------------------------------------------------------

            # root cause slots (unchanged)
            root_cause_val = None
            root_cause_type_val = None
            root_cause_subtype_val = None

            if self.root_cause_col in df_out.columns:
                root_cause_val = self._nan_to_none(row.get(self.root_cause_col))
            if self.root_cause_type_col in df_out.columns:
                root_cause_type_val = self._nan_to_none(row.get(self.root_cause_type_col))
            if self.root_cause_subtype_col in df_out.columns:
                root_cause_subtype_val = self._nan_to_none(row.get(self.root_cause_subtype_col))

            yield {
                "work_item_id": int(row[self.work_item_id_col]),
                "fields": fields,
                "comment": row.get("comment", []),
                "repro_steps": row.get("repro_steps", []),
                "additional_notes": row.get("additional_notes", []),
                "root_cause": root_cause_val,
                "root_cause_type": root_cause_type_val,
                "root_cause_subtype": root_cause_subtype_val,
            }

    def _write(self, records: Iterable[Dict[str, Any]], out_path: str) -> None:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        if self.output_format == "json":
            FileWriter.write_json(list(records), out_path, ensure_ascii=False, pretty=True)
        else:
            if not hasattr(FileWriter, "write_jsonl"):
                raise AttributeError("FileWriter.write_jsonl is missing. Please add it first.")
            FileWriter.write_jsonl(records, out_path, ensure_ascii=False)

    @staticmethod
    def _nan_to_none(x: Any) -> Any:
        if pd.isna(x):
            return None
        return x

    @staticmethod
    def _to_bool_or_none(x: Any) -> Optional[bool]:
        if x is None:
            return None
        if isinstance(x, bool):
            return x
        if isinstance(x, (int, float)) and not pd.isna(x):
            # 0/1 style
            if x == 0:
                return False
            if x == 1:
                return True
        s = str(x).strip().lower()
        if s in {"true", "t", "yes", "y", "1"}:
            return True
        if s in {"false", "f", "no", "n", "0"}:
            return False
        return None


if __name__ == "__main__":
    KEEP_FIELDS = [
        "Title",
        "Customer Name",
        "Defect Type",
        "Priority",
        "Area",
        "Family",
        "Product",
        "Subarea",
    ]

    xlsx_path = "backend/src/data/Customer Defects Created in Past 6 Months 2.xlsx"

    builder = BuildCaseJsonV3WithNotes(
        xlsx_path=xlsx_path,
        keep_fields=KEEP_FIELDS,
        output_dir="backend/src/output",
        output_name="cases_v3.jsonl",
        output_format="jsonl",
        main_sheet=" P1P2 Customer Defects",
        comments_sheet="Comments",
        repro_sheet="Repo Steps",
        notes_sheet="Notes",
        work_item_id_col="WorkItemId",
        comment_col="Comment",
        repro_col="ReproSteps",
        created_col="CreatedDate",
        modified_col="ModifiedDate",
        author_col="AuthorName",
        additional_notes_col="AdditionalNotes",
        root_cause_col="Root Cause",
        root_cause_type_col="Root Cause Type",
        root_cause_subtype_col="Root Cause Subtype",

        # V3 mapping (change these if you want a different version definition)
        product_name_col="Product",
        product_version_col="Reported Version",

        scm_col="SCM",
        code_change_col="CodeChange",
        code_change_class_col="Code Change/Non-code Change",
    )

    out_path = builder.run()
    print(f"[BuildCaseJsonV3WithNotes] Wrote: {out_path}")
