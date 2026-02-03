# Customer Defect Root Cause Automation System

## 1. Background

In enterprise product delivery and customer support, customers continuously submit Defect work items through systems such as **Salesforce** and **Azure DevOps (ADO)**. Each Defect typically contains:

- Core work item metadata (e.g., Title, Product, Priority, Area)
- Multiple rounds of communication between customers and support teams (Comments)
- Detailed reproduction or rollback steps (Steps to Repro)

Traditionally, **Root Cause analysis and classification (Type / Subtype)** are performed manually by engineers or domain experts and then written back to ADO. This approach has several limitations:

- High human cost
- Long turnaround time
- Difficult to scale
- Inconsistent classification standards across individuals

This project aims to leverage **LLMs (Copilot / GPT-class models)** combined with **Prompt Engineering** and an **automated pipeline** to generate and classify Defect root causes at scale, while supporting human review and rule-based enhancement, and ultimately writing results back to ADO in a closed loop.

---

## 2. System Objectives

- **Automated Generation**: Generate a Root Cause description for each Defect
- **Structured Classification**: Assign Root Cause Type and Subtype strictly based on official/internal definitions
- **Batch Processing**: Support large-scale, pipeline-based processing (e.g., tens of thousands of Defects)
- **Auditable & Iterative**: Enable human review, prompt refinement, and rule-based augmentation
- **Closed-loop Integration**: Write validated results back to Azure DevOps via API

---

## 3. End-to-End Pipeline Overview

### Step 1: Defect Data Extraction and Structuring

Defect data is exported or aggregated from Azure DevOps, commonly in tabular (Excel) form, and typically consists of three components:

1. **Main Sheet (one row per WorkItem)**Contains core Defect fields such as:

   - Title
   - Customer Name
   - Defect Type
   - Priority
   - Area / Family / Product / Subarea
2. **Comments Sheet (multiple rows per WorkItem)**Contains multi-round communications between customers and support engineers, optionally with metadata such as author and timestamps.
3. **Repro Steps Sheet (multiple rows per WorkItem)**
   Contains reproduction or rollback steps for the Defect.

These sources are merged into a **single structured record per WorkItem**, forming the canonical input for LLM inference.

---

### Step 2: Injecting Official Classification Definitions into Prompts

Root Cause **Type** and **Subtype** must conform to predefined ADO or internal classification standards rather than free-form generation.

The prompt constructed for each Defect includes:

- Full structured context of the Defect (fields, comments, repro steps)
- Official Root Cause Type/Subtype definitions(sourced from documents under `docs/`, such as lifecycle procedures, PDFs, or internal screenshots)
- Strict output format constraints (JSON-only, fixed keys)

This ensures the LLM output is **controlled, interpretable, and directly writable back to ADO**.

---

### Step 3: LLM Inference and Structured Output

For each Defect, the LLM generates:

- `root_cause`: Natural-language root cause explanation
- `root_cause_type`: Root cause category
- `root_cause_subtype`: Root cause subcategory

Outputs are written in **JSONL / JSON** format and **never overwrite the original input files**, ensuring traceability and reproducibility.

To control token usage and improve stability, the system supports configurable preprocessing, including:

- Limiting the number of comments sent to the LLM
- Truncating long comment or repro step text
- Limiting the number of repro steps included

If inference fails for individual cases, the system preserves the original record and attaches error information, allowing batch execution to continue uninterrupted.

---

### Step 4: Human Review and Prompt / Rule Iteration (Human-in-the-loop)

Human review is an integral part of the quality loop:

- Review samples by Product or Feature
- Validate root cause accuracy and Type/Subtype correctness
- Identify systematic failure patterns

Based on review results, the system can be iteratively improved through:

- Prompt refinement and constraint tightening
- Additional clarification or expansion of classification documentation
- Introduction of a **Rule Engine** for deterministic or high-confidence scenarios, forming a hybrid “Rules + LLM” decision framework

---

### Step 5: Writing Results Back to Azure DevOps

Once output quality meets expectations:

- Root Cause, Type, and Subtype are written back to corresponding Defect fields via Azure DevOps APIs
- The system completes a fully automated closed loop:
  **Extract → Analyze → Review → Write-back**

---

## 4. Project Structure (High-Level)

- `docs/`Root cause classification standards and lifecycle documentation (key prompt inputs)
- `backend/src/prompt/`System and user prompt templates
- `backend/src/output/`Intermediate and final artifacts (e.g., `cases.jsonl`, `cases_with_root_cause.jsonl`), typically excluded from version control
- `backend/src/pipeline/`
  Pipeline stages for data construction and LLM annotation

### Project Directory Snapshot

![1770151694156](image/README/1770151694156.png)

## 5. Input / Output Data Format Examples (Simplified)

### Input Example (Single Case)

{
  "work_item_id": 1551759,
  "fields": {
    "Title": "RPL Wizard failure, again",
    "Customer Name": "Merck",
    "Defect Type": "Does Not Work As Designed",
    "Priority": "To be set at Review",
    "Area": "Recipe Management",
    "Family": "Chemical MES",
    "Product": "Aspen Production Execution Manager",
    "Subarea": null
  },
  "comment": [
    {
      "text": "Wizard failed after FMIX MR was created.",
      "created_date": "2025-08-04T15:06:37.293Z",
      "modified_date": "2025-08-04T15:06:37.293Z",
      "author": "Dupont, Eric"
    }
  ],
  "repro_steps": [
    "Open RPL Wizard",
    "Create FMIX MR",
    "Wizard fails immediately"
  ],
  "root_cause": null,
  "root_cause_type": null,
  "root_cause_subtype": null
}

### Output Example (After LLM Annotation)

{
  "work_item_id": 1551759,
  "root_cause": "The wizard fails due to missing validation handling after FMIX MR creation.",
  "root_cause_type": "Product Defect",
  "root_cause_subtype": "Workflow / State Management"
}

## 6. How to Run (Three Commands)

From the project root directory:

pip install -r requirements.txt
python backend/src/pipeline/build_case_json.py
python backend/src/pipeline/root_cause_llm_annotator.py

Outputs will be generated under:

backend/src/output/
├── cases.jsonl
├── cases_with_root_cause.jsonl

## 7. Efficiency and Impact

Compared to fully manual workflows, this system:

* Significantly reduces human effort required for root cause analysis
* Improves consistency across Defect classifications
* Enables large-scale processing with minimal manual intervention
* Compresses multi-person, multi-week efforts into a short, review-driven cycle (often days to two weeks, depending on complexity)

## 8. Future Enhancements

* Continuous prompt and classification accuracy improvements
* Expanded rule engine coverage and validation logic
* Incremental processing and checkpoint recovery
* Enhanced auditing, metrics, and reporting before and after write-back
