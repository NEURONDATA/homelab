import os
import io
import json
import re
from typing import List, Dict, Any
from PyPDF2 import PdfReader, PdfWriter
from pydantic import BaseModel
from google import genai
from google.genai.types import Part

# ─────────────────────────  MODELS  ──────────────────────────
class LineItem(BaseModel):
    unit: str
    room: str
    category: str
    serial: str
    description: str
    qty: str
    uom: str
    reset: str
    remove: str
    replace: str
    tax: str
    oandp: str
    total: str

class BDRItems(BaseModel):
    items: List[LineItem]


# ─────────────────────────  PROMPT  ──────────────────────────
prompt = """
You are a PDF document parser. Extract line items from a construction cost table.

–––––  FIELD ORDER  (13)  –––––
[unit, room, category, serial, description, qty, uom,
 reset, remove, replace, tax, oandp, total]

–––––  FIELD DEFINITIONS  –––––
• unit      – centred bold text like “Unit 1101”; if missing → "unknown".
• room      – text beneath the unit (ignore the word CONTINUED); if missing → "unknown".
• category  – underlined heading before a block of items (e.g. “Floor(s)”).
• serial    – first number in the row, e.g. “591”.
• description – full description (may wrap to 2 lines).
• qty       – quantity such as “3.07”.
• uom       – unit of measure (EA, SF, LF, etc.) next to qty.
• reset     – numeric. May be blank **or** "0.00".  
              If blank write "0". Do **not** shift later columns.
• remove    – numeric; "0.00" is a valid value.
• replace   – numeric.
• tax       – numeric.
• oandp     – numeric (overhead & profit).
• total     – numeric (total cost).

–––––  COLUMN-COUNT RULE  –––––
After **uom** there are **exactly SIX numeric cost columns**  
[reset, remove, replace, tax, oandp, total].  
If any cell is empty insert "0".  
“0.00” counts as present, not blank.

–––––  OTHER RULES  –––––
• Serial numbers use commas (“10,952”), never periods.  
• Ignore “+”, “=”, or other math symbols.  
• Ignore any data before the first “Unit 1101”.

Return each item as JSON with the 13 keys above, in that order. Do not rename, reorder, or omit keys.
"""


# ────────────────────────  UTILITIES  ────────────────────────
def chunk_pdf(pdf_path: str, pages_per_chunk: int = 5) -> List[bytes]:
    reader = PdfReader(pdf_path)
    chunks: List[bytes] = []
    for i in range(0, len(reader.pages), pages_per_chunk):
        writer = PdfWriter()
        for page in reader.pages[i:i + pages_per_chunk]:
            writer.add_page(page)
        with io.BytesIO() as buffer:
            writer.write(buffer)
            chunks.append(buffer.getvalue())
    return chunks

def fix_json_lines(raw_json: str) -> str:
    fixed_lines = []
    pattern = re.compile(r'(^\s*"[^"]+"\s*:\s*)"((?:\\.|[^"\\])*)"\s*(,?)\s*$')
    for line in raw_json.splitlines():
        match = pattern.match(line)
        if match:
            key, value, trailing = match.groups()
            fixed_value = json.dumps(value)
            fixed_lines.append(f'{key}{fixed_value}{trailing}')
        else:
            fixed_lines.append(line)
    return "\n".join(fixed_lines)

def clean_json_string(text: str) -> str:
    text = text.replace('```json', '').replace('```', '').strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    if start != -1 and end != -1:
        text = text[start:end]
    return fix_json_lines(text)

def _to_num(s: str) -> float:
    try:
        return float(str(s).replace(",", "").strip() or "0")
    except Exception:
        return 0.0

def _needs_left_shift(item: dict) -> bool:
    total_num   = _to_num(item["total"])
    oandp_num   = _to_num(item["oandp"])
    tax_num     = _to_num(item["tax"])
    replace_num = _to_num(item["replace"])
    remove_num  = _to_num(item["remove"])
    reset_num   = _to_num(item["reset"])

    cond_a = total_num == 0 and oandp_num > 0
    cond_b = item["oandp"] == item["total"] and total_num > 0
    cond_c = item["replace"] == item["tax"]  and tax_num   > 0
    cond_d = reset_num == 0 and remove_num > 0 and replace_num > 0

    return cond_a or cond_b or cond_c or cond_d

def fix_shift(item: dict) -> dict:
    if _needs_left_shift(item):
        item = item.copy()
        item["total"]   = item["oandp"]
        item["oandp"]   = item["tax"]
        item["tax"]     = item["replace"]
        item["replace"] = item["remove"]
        item["remove"]  = "0"
    return item

def shift_on_dup(item: dict) -> dict:
    """
    If oandp == total (and non-zero), assume a one-column left shift:
    inject reset="0", push remove→replace→tax→oandp→total, drop the old total.
    """
    try:
        if item["oandp"] == item["total"] and _to_num(item["total"]) > 0:
            item = item.copy()
            item["reset"] = "0"
            item["total"]   = item["oandp"]
            item["oandp"]   = item["tax"]
            item["tax"]     = item["replace"]
            item["replace"] = item["remove"]
            item["remove"]  = "0"
    except KeyError:
        pass
    return item


# ────────────────────  MAIN PROCESSOR  ───────────────────────
def process_pdf_chunks(pdf_path: str, api_key: str, model_id: str = "gemini-2.5-flash-preview-04-17"):
    client = genai.Client(api_key=api_key)
    chunks = chunk_pdf(pdf_path)
    all_items: List[Dict[str, Any]] = []

    # initialize sticky state
    last_unit = last_room = last_category = "unknown"

    for i, chunk in enumerate(chunks):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=[Part.from_bytes(data=chunk, mime_type="application/pdf"), prompt],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": BDRItems
                }
            )
            cleaned = clean_json_string(response.text)

            json_data = json.loads(cleaned)
            if isinstance(json_data, list):
                json_data = json_data[0]
            structured = BDRItems(**json_data)

            fixed_dicts = []
            for it in structured.items:
                d = it.dict()

                # ── carry-forward state only on real new data ──
                if not d["unit"] or d["unit"].strip().lower() == "unknown":
                    d["unit"] = last_unit
                else:
                    last_unit = d["unit"]
                    print(f"[chunk {i}] 🔄 Unit state updated to: {last_unit}")

                if not d["room"] or d["room"].strip().lower() == "unknown":
                    d["room"] = last_room
                else:
                    last_room = d["room"]

                if not d["category"] or d["category"].strip().lower() == "unknown":
                    d["category"] = last_category
                else:
                    last_category = d["category"]

                # ── 1) shift if oandp == total duplicate
                d = shift_on_dup(d)
                # ── 2) safety-net for other shift cases
                d = fix_shift(d)

                fixed_dicts.append(d)

            # debug output per chunk
            with open(f"/files/2206/chunk_{i}_response_state.json", "w") as f:
                json.dump(fixed_dicts, f, indent=2)

            all_items.extend(fixed_dicts)

        except json.JSONDecodeError as e:
            print(f"⚠️  JSON decode error in chunk {i}: {e}")
            with open(f"chunk_{i}_error.txt", "w") as f:
                f.write(response.text)
        except Exception as e:
            print(f"⚠️  Gemini request failed on chunk {i}: {e}")

    # write final combined output
    final_output = {"items": all_items}
    with open("/files/2206/combined_response_2206.json", "w") as f:
        json.dump(final_output, f, indent=2)

    print("✅ Processing complete. See 'combined_response.json'.")
    return final_output


# ───────────────────────────  RUN  ───────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract line items from a construction cost table in a PDF.")
    parser.add_argument('--pdf-path', required=True, help='Path to the PDF file to process')
    args = parser.parse_args()

    pdf_path = args.pdf_path
    api_key  = os.getenv("GEMINI_API_KEY") or "AIzaSyCF3i6b2uVL08P231upan0it_Yohdl4DJ0"
    process_pdf_chunks(pdf_path, api_key)
