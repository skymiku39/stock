"""prompt_registry -- 載入 / 渲染 / 儲存 prompt YAML 模板。

設計：
* 預設讀取專案根目錄的 `prompts/*.yaml`（不含 `prompts/archive/`）
* 每份檔案對應一個 `id`，含 version / template / inputs schema
* 不在 import 時 raise — 即使 YAML 解析失敗也只是該檔被略過
* 不依賴 PyYAML 也能跑：若未安裝，會用簡易 YAML parser 撐住基本欄位
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.utils import get_logger

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False


@dataclass
class PromptInput:
    name: str
    required: bool = True
    desc: str = ""
    max_chars: Optional[int] = None


@dataclass
class PromptTemplate:
    """單一 prompt 的中繼資料。"""

    id: str
    version: str = "0"
    description: str = ""
    template: str = ""
    model_hint: str = "gemini-2.5-flash"
    max_output_tokens: int = 2048
    temperature: float = 0.2
    output_format: str = "text"
    inputs: List[PromptInput] = field(default_factory=list)
    path: Optional[Path] = None

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(self, **vars: Any) -> str:
        """以 str.format 風格替換變數，遇缺值會以空字串補上 (避免拋 KeyError)。

        若 input 設了 max_chars，會自動截斷字串避免炸 token。
        """
        for inp in self.inputs:
            if inp.max_chars and inp.name in vars and isinstance(vars[inp.name], str):
                v = vars[inp.name]
                if len(v) > inp.max_chars:
                    vars[inp.name] = v[: inp.max_chars] + "\n...(truncated)..."

        class _SafeDict(dict):
            def __missing__(self, key: str) -> str:
                return ""

        return self.template.format_map(_SafeDict(vars))

    def to_yaml_text(self) -> str:
        """把目前 in-memory 的 PromptTemplate 反序列化回 YAML 字串 (給 UI 編輯用)。"""
        if _HAS_YAML:
            data = {
                "id": self.id,
                "version": self.version,
                "description": self.description,
                "model_hint": self.model_hint,
                "max_output_tokens": self.max_output_tokens,
                "temperature": self.temperature,
                "output_format": self.output_format,
                "inputs": [
                    {
                        "name": i.name,
                        **({"required": i.required} if not i.required else {}),
                        **({"max_chars": i.max_chars} if i.max_chars else {}),
                        **({"desc": i.desc} if i.desc else {}),
                    } for i in self.inputs
                ],
                "template": self.template,
            }
            return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)  # type: ignore[attr-defined]
        # fallback: 手寫 (不完美，但能用)
        lines = [
            f"id: {self.id}",
            f"version: \"{self.version}\"",
            f"description: {self.description}",
            f"model_hint: {self.model_hint}",
            f"max_output_tokens: {self.max_output_tokens}",
            f"temperature: {self.temperature}",
            f"output_format: {self.output_format}",
            "template: |",
        ]
        for line in self.template.splitlines():
            lines.append("  " + line)
        return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# 簡易 YAML fallback (僅支援我們自己寫的格式)
# ----------------------------------------------------------------------


def _simple_yaml_parse(text: str) -> Dict[str, Any]:
    """非常簡化的 YAML parser：只支援 key: value 與 key: | 多行字串。"""
    data: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][\w]*)\s*:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "|":
            block: List[str] = []
            i += 1
            indent: Optional[int] = None
            while i < len(lines):
                blk = lines[i]
                if blk.strip() == "":
                    block.append("")
                    i += 1
                    continue
                cur_indent = len(blk) - len(blk.lstrip())
                if indent is None:
                    indent = cur_indent
                if cur_indent < (indent or 0):
                    break
                block.append(blk[(indent or 0):])
                i += 1
            data[key] = "\n".join(block)
            continue
        if val.startswith("[") and val.endswith("]"):
            inside = val[1:-1].strip()
            data[key] = [x.strip().strip('"').strip("'") for x in inside.split(",") if x.strip()]
            i += 1
            continue
        val = val.strip('"').strip("'")
        if val.lower() in ("true", "false"):
            data[key] = val.lower() == "true"
        else:
            try:
                if "." in val:
                    data[key] = float(val)
                else:
                    data[key] = int(val)
            except ValueError:
                data[key] = val
        i += 1
    return data


def _parse_yaml_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if _HAS_YAML:
        try:
            return yaml.safe_load(text) or {}  # type: ignore[attr-defined]
        except Exception:
            pass
    return _simple_yaml_parse(text)


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


class PromptRegistry:
    """全域 prompt 載入器。"""

    def __init__(
        self,
        prompts_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.prompts_dir = prompts_dir or (Path.cwd() / "prompts")
        self.logger = logger or get_logger("prompt-registry")
        self._lock = threading.Lock()
        self._prompts: Dict[str, PromptTemplate] = {}
        self.reload()

    def reload(self) -> int:
        """重新掃描 prompts 目錄。"""
        with self._lock:
            self._prompts.clear()
            if not self.prompts_dir.exists():
                self.logger.warning("prompts 目錄不存在: %s", self.prompts_dir)
                return 0
            for f in sorted(self.prompts_dir.glob("*.yaml")):
                try:
                    raw = _parse_yaml_file(f)
                    pid = str(raw.get("id") or f.stem)
                    inputs_raw = raw.get("inputs") or []
                    inputs: List[PromptInput] = []
                    if isinstance(inputs_raw, list):
                        for x in inputs_raw:
                            if isinstance(x, dict):
                                inputs.append(PromptInput(
                                    name=str(x.get("name", "")),
                                    required=bool(x.get("required", True)),
                                    desc=str(x.get("desc", "")),
                                    max_chars=x.get("max_chars"),
                                ))
                    p = PromptTemplate(
                        id=pid,
                        version=str(raw.get("version", "0")),
                        description=str(raw.get("description", "")),
                        template=str(raw.get("template", "")),
                        model_hint=str(raw.get("model_hint", "gemini-2.5-flash")),
                        max_output_tokens=int(raw.get("max_output_tokens", 2048)),
                        temperature=float(raw.get("temperature", 0.2)),
                        output_format=str(raw.get("output_format", "text")),
                        inputs=inputs,
                        path=f,
                    )
                    self._prompts[pid] = p
                except Exception:
                    self.logger.exception("解析 prompt 失敗: %s", f.name)
            self.logger.info(
                "PromptRegistry 載入完成 (%d 個 prompt) from %s",
                len(self._prompts), self.prompts_dir,
            )
            return len(self._prompts)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------

    def list_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._prompts.keys())

    def get(self, prompt_id: str) -> Optional[PromptTemplate]:
        with self._lock:
            return self._prompts.get(prompt_id)

    def render(self, prompt_id: str, **vars: Any) -> str:
        p = self.get(prompt_id)
        if p is None:
            raise KeyError(f"Prompt 不存在: {prompt_id}")
        return p.render(**vars)

    def save_yaml(self, prompt_id: str, yaml_text: str) -> Path:
        """寫回 YAML 檔；不解析失敗的內容仍會落地，但會 warn。"""
        path = self.prompts_dir / f"{prompt_id}.yaml"
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml_text, encoding="utf-8")
        self.reload()
        return path

    def __contains__(self, prompt_id: str) -> bool:
        return prompt_id in self._prompts


# ----------------------------------------------------------------------
# Singleton 工具
# ----------------------------------------------------------------------

_registry: Optional[PromptRegistry] = None
_registry_lock = threading.Lock()


def get_registry(prompts_dir: Optional[Path] = None) -> PromptRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = PromptRegistry(prompts_dir=prompts_dir)
    return _registry


__all__ = [
    "PromptInput",
    "PromptRegistry",
    "PromptTemplate",
    "get_registry",
]
