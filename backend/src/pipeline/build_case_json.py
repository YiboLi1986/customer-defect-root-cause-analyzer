import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from typing import List, Dict, Any, Iterable, Union
import pandas as pd

from backend.src.data_io.file_reader import FileReader
from backend.src.data_io.file_writer import FileWriter


class BuildCaseJson:
    """
    Build structured case JSONL/JSON from an Excel file containing:
      - Sheet1: main ADO work item fields (one row per WorkItemId)
      - Sheet2: comments (multiple rows per WorkItemId)
      - Sheet3: repro steps (multiple rows per WorkItemId)

    Output schema (per work item):
      {
        "work_item_id": 1551759,
        "fields": {
          "Title": "...",
          "Customer Name": "...",
          "Defect Type": "...",
          "Priority": "...",
          "Area": "...",
          "Family": "...",
          "Product": "...",
          "Subarea": "..."
        },
        "comment": [
          {"text": "...", "created_date": "...", "modified_date": "...", "author": "..."},
          ...
        ],
        "repro_steps": ["...", "..."],
        "root_cause": null,
        "root_cause_type": null,
        "root_cause_subtype": null
      }

    Notes:
      - "root_cause", "root_cause_type", and "root_cause_subtype" are pre-allocated slots.
      - If Sheet1 already has values for those columns, they will be copied into these slots (otherwise null).
    """

    def __init__(
        self,
        xlsx_path: str,
        keep_fields: List[str],
        output_dir: str,
        output_name: str = "cases.jsonl",
        output_format: str = "jsonl",  # "jsonl" or "json"
        main_sheet: Union[str, int] = " P1P2 Customer Defects",
        comments_sheet: Union[str, int] = "Comments",
        repro_sheet: Union[str, int] = "Repo Steps",
        work_item_id_col: str = "WorkItemId",
        # sheet2 (comments)
        comment_col: str = "Comment",
        created_col: str = "CreatedDate",
        modified_col: str = "ModifiedDate",
        author_col: str = "AuthorName",
        # sheet3 (repro)
        repro_col: str = "ReproSteps",
        # main sheet root cause columns (used to fill slots if present)
        root_cause_col: str = "Root Cause",
        root_cause_type_col: str = "Root Cause Type",
        root_cause_subtype_col: str = "Root Cause Subtype",
    ) -> None:
        """
        Args:
            xlsx_path: Excel path (can be full relative path from repo root).
            keep_fields: Explicit list of columns to keep from the main sheet.
                         (Strongly recommended to pass only the minimal set.)
            output_dir: Directory to write output files (e.g., "backend/src/output").
            output_name: Output filename, e.g. "cases.jsonl".
            output_format: "jsonl" (recommended) or "json".
            main_sheet/comments_sheet/repro_sheet: Sheet names or indices.
            work_item_id_col: Column name for the work item id across all sheets.
            comment_col/repro_col: Content columns in sheets 2 and 3.
            created_col/modified_col/author_col: Optional metadata columns for comments.
            root_cause_col/root_cause_type_col/root_cause_subtype_col: Main-sheet columns for root cause slots.
        """
        self.xlsx_path = xlsx_path
        self.keep_fields = keep_fields
        self.output_dir = output_dir
        self.output_name = output_name
        self.output_format = output_format.lower().strip()

        self.main_sheet = main_sheet
        self.comments_sheet = comments_sheet
        self.repro_sheet = repro_sheet

        self.work_item_id_col = work_item_id_col

        self.comment_col = comment_col
        self.created_col = created_col
        self.modified_col = modified_col
        self.author_col = author_col

        self.repro_col = repro_col

        self.root_cause_col = root_cause_col
        self.root_cause_type_col = root_cause_type_col
        self.root_cause_subtype_col = root_cause_subtype_col

    # ---------------- Public API ----------------

    def run(self) -> str:
        """
        Execute: read sheets -> aggregate -> merge -> write JSONL/JSON.

        Returns:
            Output file path.
        """
        df_main, df_cmt, df_rep = self._read_sheets()
        df_main, df_cmt, df_rep = self._normalize_ids(df_main, df_cmt, df_rep)

        keep_fields = self._validate_keep_fields(df_main)
        cmt_group = self._aggregate_comments(df_cmt)
        rep_group = self._aggregate_repro_steps(df_rep)

        df_out = self._merge(df_main, keep_fields, cmt_group, rep_group)
        records = self._iter_records(df_out, keep_fields)

        out_path = os.path.join(self.output_dir, self.output_name)
        self._write(records, out_path)
        return out_path

    # ---------------- Internals ----------------

    def _read_sheets(self) -> List[pd.DataFrame]:
        df_main = FileReader.read_xlsx(self.xlsx_path, sheet_name=self.main_sheet)
        df_cmt = FileReader.read_xlsx(self.xlsx_path, sheet_name=self.comments_sheet)
        df_rep = FileReader.read_xlsx(self.xlsx_path, sheet_name=self.repro_sheet)
        return [df_main, df_cmt, df_rep]

    def _normalize_ids(self, df_main: pd.DataFrame, df_cmt: pd.DataFrame, df_rep: pd.DataFrame) -> List[pd.DataFrame]:
        for df, name in [(df_main, "main"), (df_cmt, "comments"), (df_rep, "repro")]:
            if self.work_item_id_col not in df.columns:
                raise ValueError(f"Missing '{self.work_item_id_col}' in {name} sheet.")

        # normalize to int64 for stable merges
        df_main[self.work_item_id_col] = df_main[self.work_item_id_col].astype("int64")
        df_cmt[self.work_item_id_col] = df_cmt[self.work_item_id_col].astype("int64")
        df_rep[self.work_item_id_col] = df_rep[self.work_item_id_col].astype("int64")
        return [df_main, df_cmt, df_rep]

    def _validate_keep_fields(self, df_main: pd.DataFrame) -> List[str]:
        """
        Strong schema validation: missing columns => fail fast.
        """
        missing = [c for c in self.keep_fields if c not in df_main.columns]
        if missing:
            raise ValueError(f"Missing columns in main sheet: {missing}")
        return self.keep_fields

    def _aggregate_comments(self, df_cmt: pd.DataFrame) -> pd.DataFrame:
        """
        Aggregate comments per WorkItemId into list[dict].
        """
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
        """
        Aggregate repro steps per WorkItemId into list[str].
        """
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

    def _merge(
        self,
        df_main: pd.DataFrame,
        keep_fields: List[str],
        cmt_group: pd.DataFrame,
        rep_group: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Merge aggregated comment/repro_steps into main sheet.
        """
        base_cols = [self.work_item_id_col] + keep_fields

        # also keep root cause columns if present (to fill slots), without forcing in keep_fields
        extra_cols: List[str] = []
        if self.root_cause_col in df_main.columns:
            extra_cols.append(self.root_cause_col)
        if self.root_cause_type_col in df_main.columns:
            extra_cols.append(self.root_cause_type_col)
        if self.root_cause_subtype_col in df_main.columns:
            extra_cols.append(self.root_cause_subtype_col)

        df_out = df_main[base_cols + extra_cols].copy()

        if not cmt_group.empty:
            df_out = df_out.merge(cmt_group, on=self.work_item_id_col, how="left")
        else:
            df_out["comment"] = None

        if not rep_group.empty:
            df_out = df_out.merge(rep_group, on=self.work_item_id_col, how="left")
        else:
            df_out["repro_steps"] = None

        df_out["comment"] = df_out["comment"].apply(lambda x: x if isinstance(x, list) else [])
        df_out["repro_steps"] = df_out["repro_steps"].apply(lambda x: x if isinstance(x, list) else [])
        return df_out

    def _iter_records(self, df_out: pd.DataFrame, keep_fields: List[str]) -> Iterable[Dict[str, Any]]:
        """
        Stream records to avoid holding all JSON in memory (good for thousands+).
        """
        for _, row in df_out.iterrows():
            fields = {k: self._nan_to_none(row.get(k)) for k in keep_fields}

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
                "comment": row["comment"],
                "repro_steps": row["repro_steps"],
                # pre-allocated slots for LLM + review + write-back
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

    xlsx_path = "backend/src/data/Customer Defects Created in Past 6 Months.xlsx"

    builder = BuildCaseJson(
        xlsx_path=xlsx_path,
        keep_fields=KEEP_FIELDS,
        output_dir="backend/src/output",
        output_name="cases.jsonl",
        output_format="jsonl",
        main_sheet=" P1P2 Customer Defects",
        comments_sheet="Comments",
        repro_sheet="Repo Steps",
        work_item_id_col="WorkItemId",
        comment_col="Comment",
        repro_col="ReproSteps",
        created_col="CreatedDate",
        modified_col="ModifiedDate",
        author_col="AuthorName",
        root_cause_col="Root Cause",
        root_cause_type_col="Root Cause Type",
        root_cause_subtype_col="Root Cause Subtype",
    )

    out_path = builder.run()
    print(f"[BuildCaseJson] Wrote: {out_path}")
