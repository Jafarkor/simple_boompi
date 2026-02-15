"""Модуль генерации кода с сохранением в файлы"""
import logging
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class CodeGenerator:
    """Генератор файлов с кодом"""

    EXTENSIONS = {
        'python': '.py', 'javascript': '.js', 'typescript': '.ts',
        'java': '.java', 'cpp': '.cpp', 'c': '.c', 'csharp': '.cs',
        'go': '.go', 'rust': '.rs', 'php': '.php', 'ruby': '.rb',
        'swift': '.swift', 'kotlin': '.kt', 'html': '.html',
        'css': '.css', 'sql': '.sql', 'shell': '.sh', 'bash': '.sh'
    }

    STANDARD_NAMES = {
        'python': 'main.py', 'javascript': 'index.js', 'typescript': 'index.ts',
        'html': 'index.html', 'css': 'styles.css', 'java': 'Main.java',
        'cpp': 'main.cpp', 'c': 'main.c', 'go': 'main.go', 'rust': 'main.rs'
    }

    def __init__(self, output_dir: str = "code_files"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

    def _extract_code(self, response: str) -> Tuple[Optional[str], Optional[str]]:
        """Извлекает код из markdown блока"""
        # С языком: ```python\ncode```
        pattern = r'```(\w+)\n(.*?)```'
        matches = re.findall(pattern, response, re.DOTALL)
        if matches:
            lang, code = matches[0]
            return code.strip(), lang.lower()

        # Без языка: ```\ncode```
        pattern = r'```\n(.*?)```'
        matches = re.findall(pattern, response, re.DOTALL)
        if matches:
            return matches[0].strip(), None

        return None, None

    def _detect_language(self, code: str) -> Optional[str]:
        """Определяет язык по содержимому"""
        indicators = {
            'python': ['def ', 'import ', 'print(', 'class '],
            'javascript': ['function ', 'const ', 'let ', 'var '],
            'java': ['public class', 'public static void main'],
            'cpp': ['#include', 'std::'],
            'php': ['<?php'],
            'rust': ['fn main', 'let mut'],
            'go': ['package main', 'func main'],
            'html': ['<!doctype', '<html']
        }

        code_lower = code.lower()
        for lang, patterns in indicators.items():
            if any(p in code_lower for p in patterns):
                return lang
        return None

    def _generate_filename(self, language: Optional[str]) -> str:
        """Генерирует имя файла с UUID"""
        if language and language in self.STANDARD_NAMES:
            base = self.STANDARD_NAMES[language]
        else:
            ext = self.EXTENSIONS.get(language, '.txt')
            base = f"code{ext}"

        name, ext = base.rsplit('.', 1)
        unique_id = str(uuid.uuid4())[:8]
        return f"{name}_{unique_id}.{ext}", f"{name}.{ext}"

    def create_file(self, code_response: str) -> Optional[Path]:
        """Создает файл с кодом из ответа модели"""
        try:
            code, language = self._extract_code(code_response)

            if not code:
                logger.warning("No code block found")
                return None

            if not language:
                language = self._detect_language(code)

            filename, finame = self._generate_filename(language)
            filepath = self.output_dir / filename
            filepath.write_text(code, encoding='utf-8')

            logger.info(f"Code file created: {filepath}")
            return filepath, finame

        except Exception as e:
            logger.error(f"Error creating code file: {e}")
            return None
