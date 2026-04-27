"""Генерация файлов с кодом из ответов LLM."""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class CodeGenerator:
    """Извлекает код из markdown-блока и сохраняет в файл с осмысленным расширением."""

    EXTENSIONS = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "java": ".java", "cpp": ".cpp", "c": ".c",
        "csharp": ".cs", "cs": ".cs",
        "go": ".go", "rust": ".rs", "rs": ".rs",
        "php": ".php", "ruby": ".rb", "rb": ".rb",
        "swift": ".swift", "kotlin": ".kt", "kt": ".kt",
        "html": ".html", "css": ".css",
        "sql": ".sql", "shell": ".sh", "bash": ".sh", "sh": ".sh",
        "json": ".json", "yaml": ".yaml", "yml": ".yml",
    }

    STANDARD_NAMES = {
        "python": "main.py", "javascript": "index.js", "typescript": "index.ts",
        "html": "index.html", "css": "styles.css",
        "java": "Main.java", "cpp": "main.cpp", "c": "main.c",
        "go": "main.go", "rust": "main.rs",
    }

    def __init__(self, output_dir: str = "code_files"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

    def _extract_code(self, response: str) -> Tuple[Optional[str], Optional[str]]:
        """Возвращает (code, language) из markdown-блока."""
        # ```язык\nкод\n```
        if m := re.search(r"```(\w+)\s*\n(.*?)```", response, re.DOTALL):
            return m.group(2).strip(), m.group(1).lower()

        # ```\nкод\n```
        if m := re.search(r"```\s*\n(.*?)```", response, re.DOTALL):
            return m.group(1).strip(), None

        # На всякий случай — без markdown, если ответ это ровно код
        stripped = response.strip()
        if stripped:
            return stripped, None

        return None, None

    def _detect_language(self, code: str) -> Optional[str]:
        indicators = {
            "python": ["def ", "import ", "print(", "class ", "if __name__"],
            "javascript": ["function ", "const ", "let ", "var ", "=>"],
            "typescript": ["interface ", ": string", ": number", "type "],
            "java": ["public class", "public static void main"],
            "cpp": ["#include", "std::", "int main("],
            "c": ["#include", "int main("],
            "php": ["<?php"],
            "rust": ["fn main", "let mut", "impl "],
            "go": ["package main", "func main", "package "],
            "html": ["<!doctype", "<html", "<!DOCTYPE"],
            "css": ["{ ", "color:", "margin:"],
            "sh": ["#!/bin/bash", "#!/bin/sh"],
        }
        code_lower = code.lower()
        for lang, patterns in indicators.items():
            if any(p.lower() in code_lower for p in patterns):
                return lang
        return None

    def _generate_filename(self, language: Optional[str]) -> Tuple[str, str]:
        """Возвращает (имя_на_диске_с_uuid, имя_для_пользователя)."""
        if language and language in self.STANDARD_NAMES:
            base = self.STANDARD_NAMES[language]
        else:
            ext = self.EXTENSIONS.get(language or "", ".txt")
            base = f"code{ext}"

        name, ext = base.rsplit(".", 1)
        unique_id = uuid.uuid4().hex[:8]
        return f"{name}_{unique_id}.{ext}", f"{name}.{ext}"

    def create_file(self, code_response: str) -> Tuple[Optional[Path], Optional[str]]:
        """Возвращает (filepath, display_name) или (None, None) при ошибке."""
        try:
            code, language = self._extract_code(code_response)
            if not code:
                logger.warning("No code block found in response")
                return None, None

            if not language:
                language = self._detect_language(code)

            filename, display_name = self._generate_filename(language)
            filepath = self.output_dir / filename
            filepath.write_text(code, encoding="utf-8")

            logger.info(f"Code file created: {filepath} ({len(code)} chars)")
            return filepath, display_name

        except Exception as e:
            logger.exception(f"Failed to create code file: {e}")
            return None, None
