from __future__ import annotations

# rubik_core/ingestion/loader.py
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import docx  # python-docx
import fitz  # PyMuPDF
import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class RawDocument(BaseModel):
    doc_id: str
    text: str
    metadata: dict[str, Any]


class DocumentLoader:
    def __init__(self):
        # Taxonomy keywords for heuristic extraction.
        # Cross-industry frameworks and business functions only: Cubie ships to
        # any company, so no one industry's regulators or service lines belong
        # here.
        self.regulator_keywords = [
            "GDPR",
            "HIPAA",
            "SOX",
            "SOC 2",
            "ISO 27001",
            "PCI DSS",
        ]
        self.service_line_keywords = [
            "Sales",
            "Marketing",
            "Customer Support",
            "Finance",
            "Operations",
            "Human Resources",
            "Engineering",
        ]

    def generate_doc_id(self, file_path: Path) -> str:
        """Deterministic ID based on path, size, and mtime."""
        stat = file_path.stat()
        unique_str = f"{file_path.absolute()}_{stat.st_size}_{stat.st_mtime}"
        return hashlib.sha256(unique_str.encode()).hexdigest()

    def _extract_sidecar_metadata(self, file_path: Path) -> dict[str, Any]:
        """Check for .meta.json or .meta.yaml files."""
        for suffix in [".meta.json", ".meta.yaml"]:
            meta_path = file_path.with_suffix(file_path.suffix + suffix)
            if meta_path.exists():
                try:
                    with open(meta_path) as f:
                        if suffix == ".meta.json":
                            return json.load(f)
                        else:
                            return yaml.safe_load(f)
                except Exception as e:
                    logger.warning(
                        f"Failed to load sidecar metadata at {meta_path}: {e}"
                    )
        return {}

    def _parse_filename(self, filename: str) -> dict[str, Any]:
        """
        Parse filename by convention: Client__ServiceLine__Region__YYYY-MM-DD__Topic.pdf
        """
        meta = {}
        parts = filename.split("__")
        if len(parts) >= 2:
            meta["client_name"] = parts[0]
            meta["service_line"] = parts[1]
        if len(parts) >= 4:
            meta["region"] = parts[2]
            meta["engagement_date"] = parts[3]
        return meta

    def _heuristic_metadata(self, text: str) -> dict[str, Any]:
        """Basic regex/keyword extraction from first part of text."""
        meta = {"regulator_tags": []}
        header = text[:2000]  # Look at first 2000 chars

        # Regulator tags
        for kw in self.regulator_keywords:
            if re.search(rf"\b{kw}\b", header, re.IGNORECASE):
                meta["regulator_tags"].append(kw)

        # Service line
        for kw in self.service_line_keywords:
            if re.search(rf"\b{kw}\b", header, re.IGNORECASE):
                meta["service_line"] = kw
                break

        # Date heuristic (ISO format)
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", header)
        if date_match:
            meta["engagement_date"] = date_match.group(1)

        return meta

    def load_file(self, file_path: Path) -> RawDocument | None:
        if not file_path.exists():
            return None

        ext = file_path.suffix.lower()
        text = ""
        pages_count = 0

        try:
            if ext == ".pdf":
                with fitz.open(file_path) as doc:
                    pages_count = len(doc)
                    for page in doc:
                        text += page.get_text()
            elif ext == ".docx":
                doc = docx.Document(file_path)
                # Approximate page count isn't directly available in docx structure
                # We could estimate or just set to 1
                pages_count = len(doc.sections)
                full_text = []
                for para in doc.paragraphs:
                    full_text.append(para.text)
                text = "\n".join(full_text)
            elif ext in [".txt", ".md"]:
                with open(file_path, encoding="utf-8") as f:
                    text = f.read()
            elif ext == ".pptx":
                # The slide text, read the way Ask Cubie reads an attached deck
                # (zip size-checked, XML parsed with defusedxml). Vertex RAG
                # parses PPTX itself on import; this only has to say the file
                # is readable and give the AI description something to read.
                from rubik_core.chat_files.extract import pptx_text

                text, pages_count = pptx_text(file_path.read_bytes())
            elif ext == ".json":
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)

                # Check for UiPath DU standard ExtractionResult schema
                if (
                    isinstance(data, dict)
                    and "Results" in data
                    and "Document" in data["Results"]
                    and "Fields" in data["Results"]["Document"]
                ):
                    fields = data["Results"]["Document"]["Fields"]
                    semantic_sentences = []
                    for field in fields:
                        field_name = field.get("FieldName", "Unknown Field")
                        values = field.get("Values", [])
                        if values:
                            # Usually there's one value, but handle multiple just in case
                            val_strs = [
                                str(v.get("Value", ""))
                                for v in values
                                if "Value" in v and v["Value"] is not None
                            ]
                            if val_strs:
                                combined_val = ", ".join(val_strs)
                                semantic_sentences.append(
                                    f"The extracted {field_name.replace('_', ' ')} is {combined_val}."
                                )

                    if semantic_sentences:
                        text = "\n".join(semantic_sentences)
                        logger.info(
                            f"Extracted {len(semantic_sentences)} semantic fields from UiPath DU json"
                        )
                    else:
                        text = json.dumps(
                            data, indent=2
                        )  # Fallback if Fields list is empty or unparsable
                else:
                    # Generic JSON flattening fallback
                    def flatten_json(y):
                        out = {}

                        def flatten(x, name=""):
                            if type(x) is dict:
                                for a in x:
                                    flatten(x[a], name + a + "_")
                            elif type(x) is list:
                                i = 0
                                for a in x:
                                    flatten(a, name + str(i) + "_")
                                    i += 1
                            else:
                                out[name[:-1]] = x

                        flatten(y)
                        return out

                    flat = flatten_json(data)
                    sentences = [
                        f"The {k.replace('_', ' ')} is {v}."
                        for k, v in flat.items()
                        if v
                    ]
                    text = "\n".join(sentences)

            else:
                logger.warning(f"Unsupported file type: {ext}")
                return None
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return None

        if not text.strip():
            logger.warning(f"No text extracted from {file_path}")
            return None

        doc_id = self.generate_doc_id(file_path)

        # Metadata capture precedence
        metadata = {
            "source_path": str(file_path),
            "file_type": ext,
            "doc_id": doc_id,
            "ingested_at": datetime.utcnow().isoformat(),
            "pages_total": pages_count,
            "original_filename": file_path.name,
        }

        # 1. Filename parsing (initial)
        metadata.update(self._parse_filename(file_path.stem))

        # 2. Heuristics from text
        metadata.update(self._heuristic_metadata(text))

        # 3. Sidecar (highest precedence)
        metadata.update(self._extract_sidecar_metadata(file_path))

        # 4. Final metadata cleanup: ensure no lists (some vector stores like Chroma don't support them)
        final_meta = {}
        for k, v in metadata.items():
            if isinstance(v, list):
                final_meta[k] = ", ".join(map(str, v))
            else:
                final_meta[k] = v

        return RawDocument(doc_id=doc_id, text=text, metadata=final_meta)
